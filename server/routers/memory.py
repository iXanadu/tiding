import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from server.dependencies import (
    check_namespace_access,
    check_namespaces_access,
    get_current_principal,
    resolve_read_namespaces,
)
from server.models import (
    MemorySupersedeRequest,
    MemorySupersedeResponse,
    InboxAckRequest,
    InboxAckResponse,
    InboxBanner,
    InboxListRequest,
    InboxListResponse,
    InboxUnreadSender,
    InboxUnreadSummaryRequest,
    InboxUnreadSummaryResponse,
    InboxResolveRequest,
    InboxResolveThreadRequest,
    InboxResolveThreadResponse,
    InboxSendRequest,
    InboxSendResponse,
    InboxWaitRequest,
    InboxWaitResponse,
    MemoryFlagDeletionRequest,
    MemoryFlagDeletionResponse,
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryGetRequest,
    MemoryGetResponse,
    MemoryKeysRequest,
    MemoryKeysResponse,
    KeyEntry,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySetRequest,
    MemorySetResponse,
    PresenceUpdateRequest,
    PresenceUpdateResponse,
    RosterEntry,
    RosterRequest,
    RosterResponse,
)
from server.services.audit_service import audit
from server.services.identity import autocorrect_address, validate_listen_set
from server.services.session_registry import SEAT_SCOPE, unknown_root_advisories
from server.services.inbox_guidance import (
    ack_guidance,
    archive_guidance,
    inbox_list_guidance,
    resolve_guidance,
    send_guidance,
)
from server.services.memory_service import (
    INBOX_NAMESPACE,
    INBOX_SCOPE,
    PRESENCE_SCOPE,
    ForgetDenied,
    OwnershipConflict,
    VersionConflict,
    inbox_ack,
    inbox_archive,
    inbox_banner,
    inbox_counts,
    inbox_unread_by_sender,
    inbox_list,
    inbox_resolve,
    inbox_resolve_thread,
    inbox_send,
    memory_flag_deletion,
    memory_forget,
    memory_get,
    memory_keys,
    memory_search,
    memory_set,
    memory_supersede,
    partition_siblings,
    presence_farewell,
    presence_update,
    presence_watcher_beat,
    recipient_liveness,
    roster_list,
)

logger = logging.getLogger(__name__)

# Inbox, presence and seat rows have their own lifecycle endpoints (send/ack/
# resolve/archive; presence heartbeat; seat claim/release). The generic
# set/forget path must NOT reach them — otherwise a writer could overwrite a
# message body (wiping read_by / from_principal), delete mail outside its
# lifecycle, or hand itself an address the registry believes someone else holds.
_RESERVED_SCOPES = {INBOX_SCOPE, PRESENCE_SCOPE, SEAT_SCOPE}


def _reject_reserved_scope(scope: str | None) -> None:
    if scope in _RESERVED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope '{scope}' is managed by its own endpoints "
                   f"(inbox: /memory/send, presence: /memory/presence, "
                   f"seat: /session/claim) — "
                   f"not writable via /memory/set or /memory/forget.",
        )

router = APIRouter(prefix="/memory", tags=["memory"])


async def _probe_namespaces(principal, fallback_ns: str) -> list[str]:
    """Namespaces to probe for same-key siblings.

    The probe is advisory, so it must never make a request fail that would
    otherwise succeed: an anonymous caller (legacy no-auth mode) has no
    principal to resolve, and resolve_read_namespaces correctly refuses to
    guess for search — but here the honest fallback is simply the namespace
    the request itself named.
    """
    if principal is None:
        return [fallback_ns]
    return await resolve_read_namespaces(principal)


@router.post("/set", response_model=MemorySetResponse)
async def set_memory(req: MemorySetRequest, request: Request):
    logger.debug(f"SET ns={req.namespace} key={req.key} user_id={req.user_id} scope={req.scope}")
    _reject_reserved_scope(req.scope)
    principal = get_current_principal(request)
    check_namespace_access(principal, req.namespace, "write")
    metadata = {}
    if principal:
        metadata["principal"] = principal["name"]
    machine = request.headers.get("x-engram-machine")
    if machine:
        metadata["machine"] = machine
    project = request.headers.get("x-engram-project")
    if project:
        metadata["project"] = project
    cwd = request.headers.get("x-engram-cwd")
    if cwd:
        metadata["cwd"] = cwd
    # MODEL-RECORD-1: which MODEL wrote this row. `principal` says who
    # authenticated and the provider says which harness drove — neither
    # answers what was thinking, and nothing did until now.
    #
    # Recorded PER ROW rather than on the session, because a session is not one
    # model: measured 2026-08-09, 45 of 237 transcripts on one box changed model
    # mid-session, one of them after 29 turns and then for 549 more. A
    # session-level stamp would have confidently misattributed whichever side of
    # that switch it missed, and a confident wrong provenance is worse than none.
    #
    # `model_source` is stored even when the model is unknown, so a reader can
    # always tell "we looked and there was nothing to read" from "nobody looked".
    model_source = request.headers.get("x-engram-model-source")
    if model_source:
        metadata["model_source"] = model_source
    model = request.headers.get("x-engram-model")
    if model:
        metadata["model"] = model
    owner = principal["name"] if principal else None
    try:
        key, created, version = await memory_set(
            namespace=req.namespace,
            key=req.key,
            value=req.value,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
            tags=req.tags,
            tags_search=req.tags_search,
            expiration_days=req.expiration_days,
            metadata=metadata or None,
            owner=owner,
            if_match=req.if_match,
            actor_is_admin=bool(principal and principal.get("is_admin")),
        )
        # AUDIT-1: the write trail. Overwrites matter most — "created": false
        # is the overwrite that no backup window could reconstruct.
        await audit("memory.set", principal, {
            "namespace": req.namespace, "key": req.key, "scope": req.scope,
            "user_id": req.user_id, "project": req.project,
            "created": created,
        })
        banner = None
        if req.listen_set:
            try:
                listen_set = validate_listen_set(req.listen_set)
            except ValueError:
                listen_set = []
            if listen_set:
                banner_dict = await inbox_banner(
                    listen_set=listen_set,
                    reader_identity=req.reader_identity,
                )
                if banner_dict:
                    banner = InboxBanner(**banner_dict)
        # SEC-7 (warn, locked 2026-07-27): unknown request fields are almost
        # always a misspelled option. The write went through WITHOUT them —
        # say so, by name, instead of letting the caller debug a guard that
        # silently never runs.
        extras = sorted((req.model_extra or {}).keys())
        warning = (
            f"unknown fields ignored: {', '.join(extras)} — check spelling "
            f"(e.g. the concurrency guard is 'if_match')"
        ) if extras else None
        # MEM-3 honesty: writing to a key another writer also holds in this
        # project says "Stored" and silently forks a duplicate. The write is
        # legitimate (partitions exist on purpose) — the SILENCE was not:
        # measured 2026-08-10, an agent overrode 7 stale keys believing it had
        # replaced them, and both rows then competed in search unmarked.
        fork_warnings: list[str] = []
        if req.scope == "project":
            readable = await _probe_namespaces(principal, req.namespace)
            others = await partition_siblings(
                readable, req.key, req.project, exclude_user_id=req.user_id
            )
            fork_warnings = [
                f"'{req.key}' ALSO exists in this project under writer '{w}'. "
                f"Your write updated only YOUR row — both now rank in search. "
                f"If theirs is stale, retire it: memory/supersede with "
                f"target_user_id='{w}'."
                for w in others
            ]
        return MemorySetResponse(
            status="ok",
            key=key,
            namespace=req.namespace,
            created=created,
            version=version,
            partition_warnings=fork_warnings,
            # Positive confirmation that the guard ran. A client MUST treat
            # anything other than True as "not guarded" — on a server that
            # predates MEM-4 this field is simply absent, which is precisely
            # the case that would otherwise pass unguarded while looking safe.
            if_match_applied=req.if_match is not None,
            warning=warning,
            inbox_banner=banner,
        )
    except OwnershipConflict as e:
        # 409, and it NAMES the holder. A bare "denied" cannot be told apart
        # from a partition mistake, which is the failure this project has paid
        # for repeatedly — an answer that is technically correct and leaves the
        # caller unable to act on it. Knowing the owner is not a disclosure:
        # the row was already readable, and its writer is already shown on
        # every search result.
        await audit("memory.set.refused", principal, {
            "namespace": req.namespace, "key": req.key, "scope": req.scope,
            "project": req.project, "owner": e.current_owner,
            "attempted_by": e.attempted_by,
        })
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ownership_conflict",
                "message": (
                    f"'{req.key}' in project '{req.project}' was written by "
                    f"'{e.current_owner}' and cannot be changed by "
                    f"'{e.attempted_by}'. Project memory is readable by every "
                    f"agent but writable only by its author — write your own "
                    f"key, or use scope=shared if this belongs to everyone."
                ),
                "owner": e.current_owner,
                "attempted_by": e.attempted_by,
            },
        )
    except VersionConflict as e:
        # 409 carries the CURRENT value so the caller can re-merge its section
        # immediately, without a second round trip to discover what it lost
        # the race to. This is the whole point of the guard: the losing write
        # is refused, not silently applied over someone else's edit.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "message": (
                    "the stored value changed since you read it — re-read, "
                    "re-apply your change, and retry with the new version"
                ),
                "current_value": e.current_value,
                "current_version": e.current_version,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("memory_set failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/get", response_model=MemoryGetResponse)
async def get_memory(req: MemoryGetRequest, request: Request):
    logger.debug(f"GET ns={req.namespace} key={req.key} scope={req.scope} user_id={req.user_id}")
    check_namespace_access(get_current_principal(request), req.namespace, "read")
    try:
        item = await memory_get(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
        )
        if item:
            return MemoryGetResponse(status="ok", memory=item)
        # MEM-3 honesty: a partition miss must not read as the key not
        # existing. Same key under another writer in this project → say so,
        # with the exact call that works (measured 2026-08-10: three verbs
        # reported a cross-writer collision as a clean slate).
        warnings: list[str] = []
        if req.scope == "project":
            principal = get_current_principal(request)
            readable = await _probe_namespaces(principal, req.namespace)
            others = await partition_siblings(
                readable, req.key, req.project, exclude_user_id=req.user_id
            )
            warnings = [
                f"'{req.key}' exists in this project under writer '{w}' — "
                f"your get resolved to partition '{req.user_id}'. Read theirs "
                f"with user_id='{w}'."
                for w in others
            ]
        return MemoryGetResponse(status="not_found", partition_warnings=warnings)
    except Exception as e:
        logger.exception("memory_get failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


def _snippet(item, max_lines: int):
    """Trim a search hit's value, and SAY SO in the value itself.

    Search is for finding; `memory_get` is for reading. A startup sweep of
    four searches returned the same 1,200-word handoff three times, which is
    a large slice of a context window spent re-reading text already in it.

    The marker is not decoration. A truncated value that looks whole is the
    same failure as every other one found this week — a partial answer
    presented as a complete one — so the trailing line states the omission and
    names the exact call that returns the rest.
    """
    value = getattr(item, "value", None)
    if not isinstance(value, str):
        return item
    lines = value.splitlines()
    if len(lines) <= max_lines:
        return item
    hidden = len(lines) - max_lines
    body = "\n".join(lines[:max_lines])
    key = getattr(item, "key", "<key>")
    # A project search spans every writer, but memory_get still resolves to the
    # CALLER's partition — so a bare hint is a dead end for any row the caller
    # did not write: it would return nothing, having just been advertised as
    # retrievable. Name the writer so the suggested call actually works.
    writer = getattr(item, "user_id", None)
    if getattr(item, "scope", None) == "project" and writer:
        call = f"memory_get(key={key!r}, user_id={writer!r})"
    else:
        call = f"memory_get(key={key!r})"
    return item.model_copy(update={
        "value": (
            f"{body}\n… [+{hidden} more line(s) — TRUNCATED for search. "
            f"Full text: {call}]"
        )
    })


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(req: MemorySearchRequest, request: Request):
    principal = get_current_principal(request)
    explicit = req.explicit_namespaces()
    if explicit is None:
        ns_list = await resolve_read_namespaces(principal)
        logger.debug(f"SEARCH ns=<resolved from principal:{principal.get('name') if principal else None}> count={len(ns_list)}")
    else:
        ns_list = explicit
        check_namespaces_access(principal, ns_list, "read")
    logger.debug(f"SEARCH ns={ns_list} query={req.query!r} user_id={req.user_id} scope={req.scope}")
    try:
        results = await memory_search(
            namespaces=ns_list,
            query=req.query,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
            limit=req.limit,
            include_superseded=req.include_superseded,
        )
        if req.snippet_lines:
            results = [_snippet(r, req.snippet_lines) for r in results]
        banner = None
        if req.listen_set:
            try:
                listen_set = validate_listen_set(req.listen_set)
            except ValueError:
                listen_set = []
            if listen_set:
                banner_dict = await inbox_banner(
                    listen_set=listen_set,
                    reader_identity=req.reader_identity,
                )
                if banner_dict:
                    banner = InboxBanner(**banner_dict)
        return MemorySearchResponse(status="ok", results=results, inbox_banner=banner)
    except Exception as e:
        logger.exception("memory_search failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/keys", response_model=MemoryKeysResponse)
async def list_keys(req: MemoryKeysRequest, request: Request):
    """Deterministic key enumeration under a prefix (MEM-2).

    The verb between /memory/get (exact) and /memory/search (semantic):
    every key under a prefix, key-ordered, no embedding, with a `total` so a
    truncated listing can never pass as a complete one. Exists because
    semantic search cannot establish ABSENCE — "did a shut-down agent store
    anything?" previously took direct SQL. Namespace permissions are enforced
    exactly as search enforces them.
    """
    principal = get_current_principal(request)
    explicit = req.explicit_namespaces()
    if explicit is None:
        ns_list = await resolve_read_namespaces(principal)
    else:
        ns_list = explicit
        check_namespaces_access(principal, ns_list, "read")
    logger.debug(
        f"KEYS ns={ns_list} prefix={req.prefix!r} scope={req.scope} "
        f"user_id={req.user_id}"
    )
    try:
        entries, total = await memory_keys(
            namespaces=ns_list,
            prefix=req.prefix,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
            limit=req.limit,
        )
        return MemoryKeysResponse(
            status="ok",
            keys=[KeyEntry(**e) for e in entries],
            total=total,
        )
    except Exception:
        logger.exception("memory_keys failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/forget", response_model=MemoryForgetResponse)
async def forget_memory(req: MemoryForgetRequest, request: Request):
    logger.debug(f"FORGET ns={req.namespace} key={req.key} scope={req.scope} user_id={req.user_id}")
    _reject_reserved_scope(req.scope)
    principal = get_current_principal(request)
    check_namespace_access(principal, req.namespace, "write")
    try:
        try:
            deleted = await memory_forget(
                namespace=req.namespace,
                key=req.key,
                scope=req.scope,
                user_id=req.user_id,
                project=req.project,
                actor_principal=(principal or {}).get("name"),
                actor_is_admin=bool((principal or {}).get("is_admin")),
            )
        except ForgetDenied as denied:
            # MEM-8: destruction is self-only. Refusals are audited — a
            # denied delete attempt is exactly the event the trail is for.
            await audit("memory.forget.denied", principal, {
                "namespace": req.namespace, "key": req.key,
                "scope": req.scope, "user_id": req.user_id,
                "project": req.project, "controller": denied.controller,
            })
            raise HTTPException(
                status_code=403,
                detail=(
                    f"'{req.key}' is controlled by '{denied.controller}' — "
                    f"only its controller or an admin can hard-delete it. "
                    f"To retire it, use memory/supersede (kept as history); "
                    f"to request true destruction, use memory/flag_deletion "
                    f"(hidden immediately, purged after admin review)."
                ),
            )
        status = "ok" if deleted else "not_found"
        forget_warnings: list[str] = []
        if req.scope == "project":
            readable = await _probe_namespaces(principal, req.namespace)
            others = await partition_siblings(
                readable, req.key, req.project, exclude_user_id=req.user_id
            )
            if deleted:
                # The delete SUCCEEDING is the sharper trap: an agent probing
                # a shared key deletes ITS OWN row — possibly its own
                # correction — and gets plain success. Measured 2026-08-10:
                # a session used forget as a lookup and silently destroyed
                # its own mitigation. Say whose row died and whose survive.
                forget_warnings = [
                    f"Deleted YOUR row ('{req.user_id}') — '{req.key}' STILL "
                    f"exists in this project under writer '{w}'. If you meant "
                    f"to retire theirs, that is memory/supersede, and your "
                    f"deleted row may have been a correction worth restoring."
                    for w in others
                ]
            else:
                forget_warnings = [
                    f"'{req.key}' exists in this project under writer '{w}', "
                    f"which this principal cannot delete. To retire a stale "
                    f"row you do not own, use memory/supersede (keeps it as "
                    f"history, hides it from default search)."
                    for w in others
                ]
        # AUDIT-1: deletes are the rows the trail exists for — this exact
        # call was provable but undatable during the 2026-07-25 incident.
        # Misses are recorded too: an attempted delete of a key that is not
        # there is forensically interesting in its own right.
        await audit("memory.forget", principal, {
            "namespace": req.namespace, "key": req.key, "scope": req.scope,
            "user_id": req.user_id, "project": req.project,
            "deleted": deleted,
        })
        return MemoryForgetResponse(
            status=status, key=req.key, partition_warnings=forget_warnings
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("memory_forget failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/flag_deletion", response_model=MemoryFlagDeletionResponse)
async def flag_deletion(req: MemoryFlagDeletionRequest, request: Request):
    """MEM-8: request physical destruction of a row you may not delete.

    Permission is WRITE on the namespace — broader than forget (self-only),
    narrower than supersede (read): asking for destruction is a write-class
    act. The row is hidden from default reads at flag time (a flagged
    secret's exposure ends immediately) and an admin/librarian executes or
    rejects the request from the queue.
    """
    principal = get_current_principal(request)
    check_namespace_access(principal, req.namespace, "write")
    try:
        row = await memory_flag_deletion(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
            actor_principal=(principal or {}).get("name"),
            reason=req.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await audit("memory.flag_deletion", principal, {
        "namespace": req.namespace, "key": req.key, "scope": req.scope,
        "user_id": req.user_id, "project": req.project,
        "reason": req.reason, "found": row is not None,
    })
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no row '{req.key}' at scope={req.scope} "
                f"user_id='{req.user_id}' (rows already flagged for deletion "
                f"are not re-stamped — the queue holds the first reason)"
            ),
        )
    return MemoryFlagDeletionResponse(
        status="ok",
        key=row["key"],
        namespace=row["namespace"],
        guidance=(
            "The row is hidden from default reads NOW and queued for "
            "physical deletion pending admin review. Nothing is destroyed "
            "until the librarian executes the request."
        ),
    )


@router.post("/supersede", response_model=MemorySupersedeResponse)
async def supersede_memory(req: MemorySupersedeRequest, request: Request):
    """MEM-3: retire another writer's stale row without deleting it.

    Permission is READ on the row's namespace — deliberately not write/delete.
    OWN-1 still owns the VALUE; this stamps lifecycle metadata beside it, fully
    attributed, and default search stops returning the row. Scope is 'project'
    (default) or 'shared' (MEM-7: the shared lesson corpus is the curation
    target); personal scopes are not touchable by peers. The row is kept
    verbatim (audit trail); readers wanting history pass
    include_superseded=true.
    """
    principal = get_current_principal(request)
    # READ gate, checked against the caller's whole readable set — the row may
    # live in a namespace the caller reads but does not write (that is the
    # normal cross-provider case this exists for).
    check_namespace_access(principal, req.namespace, "read")
    ns_candidates = [req.namespace]
    try:
        row = await memory_supersede(
            namespaces=ns_candidates,
            key=req.key,
            project=req.project,
            target_user_id=req.target_user_id,
            actor_principal=(principal or {}).get("name"),
            reason=req.reason,
            replacement_key=req.replacement_key,
            scope=req.scope,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # AUDIT-1: a supersede is an edit to what readers will retrieve — exactly
    # the class of action the trail exists for.
    await audit("memory.supersede", principal, {
        "key": req.key, "scope": req.scope, "project": req.project,
        "target_user_id": req.target_user_id,
        "reason": req.reason, "replacement_key": req.replacement_key,
        "found": row is not None,
    })
    if row is None:
        where = (
            "in shared scope" if req.scope == "shared"
            else f"in project '{req.project}'"
        )
        raise HTTPException(
            status_code=404,
            detail=(
                f"no live row '{req.key}' under writer '{req.target_user_id}' "
                f"{where} within your readable namespaces "
                f"(already superseded rows are not re-stamped)"
            ),
        )
    return MemorySupersedeResponse(
        status="ok",
        key=row["key"],
        target_user_id=row["user_id"],
        namespace=row["namespace"],
        guidance=(
            "The row is retired from default search but KEPT — readers can "
            "still reach it via include_superseded=true. Nothing was deleted."
        ),
    )


# --- Inbox endpoints ----------------------------------------------------

def _loose_address(address: str | None) -> str | None:
    """Reduce a reader identity to the address peers actually send to.

    Sessions are addressed loosely (``engram``) but label their outbound mail
    with the fully-qualified form (``engram@macmini``). A participant list must
    hold the loose form or a reply would be sent to an address nobody listens
    on. ``machine:<host>`` identities have no loose form and pass through.
    """
    if not address:
        return None
    address = address.strip().lower()
    if not address or address.startswith("machine:"):
        return address or None
    return address.split("@", 1)[0] or None


def _participant_set(recipients: list[str], sender: str | None) -> list[str]:
    """Everyone in a fan-out conversation: the recipients plus the sender.

    The sender is included because a group thread is only a group if replies
    reach the person who convened it — otherwise the convener is the one party
    who cannot hear the conversation they started.

    Order-preserving and de-duplicated: the list is shown to humans and
    iterated when fanning replies, so a stable, non-repeating order keeps a
    reply from being delivered twice to an address named two ways.
    """
    out: list[str] = []
    for addr in [*recipients, sender]:
        loose = _loose_address(addr)
        if loose and loose not in out:
            out.append(loose)
    return out


@router.post("/send", response_model=InboxSendResponse)
async def send_inbox(req: InboxSendRequest, request: Request):
    """Send an inbox message to a project, machine, #channel, or a list of
    recipients (ad-hoc fan-out: one message row per recipient)."""
    principal = get_current_principal(request)
    # MAIL IS NOT MEMORY — gated on READ, not write (2026-08-02).
    #
    # Writing memory deposits shared knowledge into a namespace. Sending mail
    # addresses a row to a RECIPIENT, who owns it. Gating the second on the
    # first was a conflation, and it made messaging structurally unavailable to
    # every principal that is not a shared-namespace writer: 4 of 7 could not
    # send at all, and one of them had consequently never written a row
    # anywhere. Nobody noticed, because the three that worked are the three in
    # daily use.
    #
    # Read is the honest prerequisite: you must be able to see a community to
    # message it. That is not a licence to rewrite what the community knows.
    #
    # Verified a NO-OP for every currently-active principal before shipping:
    # those that can send today all hold read as well, and those that cannot
    # (ha-system, moneymaker) hold no read here either, so they still cannot.
    # What it unblocks is SCOPED principals — a chat client that should read
    # projects and write only its owner's personal memories can now relay a
    # message without being handed write access to every shared row on the
    # fleet in order to do it.
    #
    # DELIBERATELY NOT CHANGED: POST /memory/presence. A heartbeat writes a row
    # ABOUT YOU into the namespace; it is not addressed to anyone, so the
    # argument above does not reach it. Relaxing an adjacent endpoint because
    # it sits nearby is how a scoped change becomes a hole.
    check_namespace_access(principal, INBOX_NAMESPACE, "read")
    raw_targets = req.to if isinstance(req.to, list) else [req.to]
    try:
        corrected: list[tuple[str, str | None]] = [
            autocorrect_address(t) for t in raw_targets
        ]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # HUD-1 — private multi-party threads. A fan-out is a GROUP, so record who
    # is in it and give every copy ONE thread id. Both are load-bearing:
    #
    #  * participants let `memory_reply` fan a reply back to the whole group
    #    instead of only the sender. Without it a 3-way send is really N
    #    parallel DMs with the sender as a human relay — the workaround this
    #    replaces.
    #  * a shared thread id makes those copies one conversation. Previously
    #    each fan-out row carried the caller's (often absent) thread_id, and
    #    reply fell back to the PARENT'S OWN id — which differs per recipient,
    #    so each participant's replies landed in a separate thread.
    #
    # Membership is fixed at SEND time, chosen from the live roster. That is
    # what makes ad-hoc huddles possible at all: `#channel` membership is
    # launch-time (a session's ENGRAM_CHANNELS is set before it starts), so a
    # room can never be formed around sessions that are already running. A
    # participant set needs no subscription — every session already listens on
    # its own address — so the group can be assembled after the fact.
    #
    # Deliberately only for genuine fan-out (>1 recipient): a 1:1 DM has no
    # group, and single-recipient sends stay byte-identical to before.
    is_fanout = len(corrected) > 1
    participants: list[str] | None = None
    thread_id = req.thread_id
    # A send with no self-asserted `from` label used to produce mail nobody
    # could REPLY to: memory_reply routes on the parent's from-address, so
    # the omission silently killed every reply loop (measured 2026-08-15 —
    # a surface DM'd label-less owner mail and both recipients had to break
    # threading and fresh-send to the owner instead). The server already
    # holds the verified truth, so default the label to the authenticated
    # principal's name; label and principal then agree, which is exactly the
    # state the render layer badges as verified. Anonymous mode (no
    # principal) keeps the old behavior — there is no truth to default to.
    sender_label = (req.from_ or "").strip() or (principal or {}).get("name")
    if is_fanout:
        participants = _participant_set(
            [t for t, _ in corrected], sender=sender_label
        )
        thread_id = thread_id or f"inbox/{uuid.uuid4()}"
    try:
        ids: list[str] = []
        for to, _orig in corrected:
            msg_id = await inbox_send(
                to=to,
                body=req.body,
                subject=req.subject,
                from_=sender_label,
                thread_id=thread_id,
                participants=participants,
                supersedes=req.supersedes if not ids else None,  # supersede once
                intent=req.intent,
                # Server-derived, unspoofable: taken from the authenticated principal
                # (request.state), NOT the request body — a client cannot assert
                # someone else's identity or forge owner authority (MSG-1/MSG-2).
                from_principal=(principal or {}).get("name"),
                authority=bool(principal and principal.get("is_admin")),
                machine=request.headers.get("x-engram-machine"),
                model=request.headers.get("x-engram-model"),
                model_source=request.headers.get("x-engram-model-source"),
                from_lane=req.from_lane,
                # O2: the sender's project, from the provenance header every
                # client already sends — the address a cross-project reply
                # targets (reply-to-channel). Header-derived so even
                # pre-sweep bridges stamp it; provenance, not proof.
                from_project=(
                    request.headers.get("x-engram-project") or ""
                ).strip().lower() or None,
            )
            ids.append(msg_id)
        first_to, first_corrected = corrected[0]
        if len(corrected) == 1:
            guidance = send_guidance(
                to=first_to,
                reader_identity=sender_label,
                listen_set=req.listen_set,
            )
            if first_corrected:
                guidance = (
                    f"⚠️  ADDRESS AUTO-CORRECTED: '{first_corrected}' → '{first_to}'\n"
                    f"    The ':' delimiter is reserved for 'machine:' and 'topic:' prefixes.\n"
                    f"    Use '{first_to}' (any machine) or 'name@host' (specific machine).\n"
                    f"    Your message was delivered to '{first_to}'.\n\n"
                ) + guidance
        else:
            guidance = (
                f"Fan-out: delivered to {len(corrected)} recipients "
                f"({', '.join(t for t, _ in corrected)}). Each got its own message id."
            )
        # Tell the sender NOW if a recipient that expects to be woken is not
        # there. The data has always been one query away at exactly this
        # moment; nothing looked. A peer divided work with a counterparty
        # 42 hours dead and started building its half — the roster would have
        # said so, and asking it is a step you have to remember to take.
        #
        # Scoped by INTENT, not liveness alone. Sending to a session that is
        # not running yet is legitimate and common (queued mail is a feature),
        # so `fyi` is silent by design; only a message whose purpose is
        # coordination is broken by a dead recipient.
        warnings: list[str] = []
        if (req.intent or "").lower() != "fyi":
            live = await recipient_liveness([t for t, _ in corrected])
            for addr, info in live.items():
                # Facts, not verdicts (the store attests, consumers judge):
                # warn on a stale heartbeat or a watcher that beat and then
                # went quiet. Both are stated as observations — the sender
                # decides what silence means, because the store cannot
                # (MSG-8: a busy agent and a dead one are both silent).
                if (info["is_stale"] or info["watcher_alive"] is False
                        or info.get("farewell_at")):
                    hrs = info["age_seconds"] / 3600.0
                    age = f"{hrs:.1f}h" if hrs >= 1 else f"{int(info['age_seconds'])}s"
                    facts = f"last heartbeat {age} ago"
                    if info["watcher_alive"] is False:
                        facts += ", watcher silent"
                    # The one fact here that needs no window to elapse. A seat
                    # abandoned inside the 5-minute watcher window warned about
                    # nothing before this — the exact case that cost a peer a
                    # blocking work item sent to an empty chair.
                    if info.get("farewell_at"):
                        facts += ", watcher OBSERVED the session exit"
                    warnings.append(
                        f"{addr}: {facts} — delivered and stored, but do not "
                        "expect a reply. Check memory_roster before dividing "
                        "work or handing off."
                    )
        # Step 8 typo detection (ADDR-2 doctrine: warn, never reject) —
        # fires regardless of intent: a typo'd fyi is just as lost. A known
        # root, a person, an exempt role, or any address a session has ever
        # held stays silent; only a string NOTHING roots draws the advisory.
        try:
            warnings.extend(
                await unknown_root_advisories([t for t, _ in corrected]))
        except Exception:
            logger.exception("unknown_root_advisories failed (advisory only)")
        return InboxSendResponse(
            status="ok",
            id=ids[0],
            ids=ids if len(ids) > 1 else None,
            corrected_from=first_corrected,
            guidance=guidance,
            recipient_warnings=warnings or None,
        )
    except Exception as e:
        logger.exception("inbox_send failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox", response_model=InboxListResponse)
async def list_inbox(req: InboxListRequest, request: Request):
    """List inbox messages for the given listen_set."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        listen_set = validate_listen_set(req.listen_set)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        messages = await inbox_list(
            listen_set=listen_set,
            reader_identity=req.reader_identity,
            unread_only=req.unread_only,
            limit=req.limit,
            include_resolved=req.include_resolved,
            newest_first=req.newest_first,
        )
        counts = await inbox_counts(
            listen_set=listen_set,
            reader_identity=req.reader_identity,
        )
        return InboxListResponse(
            status="ok",
            messages=messages,
            guidance=inbox_list_guidance(
                reader_identity=req.reader_identity or "(unknown)",
                listen_set=listen_set,
                msg_count=len(messages),
                stale_count=sum(1 for m in messages if m.is_stale),
                counts=counts,
            ),
        )
    except Exception as e:
        logger.exception("inbox_list failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox/unread-summary", response_model=InboxUnreadSummaryResponse)
async def inbox_unread_summary(req: InboxUnreadSummaryRequest, request: Request):
    """Who has DIRECT mail waiting for this reader, and how much.

    Built for a per-correspondent badge ("this agent has something for you").
    DIRECT ONLY — fan-out threads and `huddle/...` relay threads are excluded,
    because "unread" does not mean one thing in a multi-party conversation.

    Deliberately server-side: "unread" is a DEFINITION. Assembled separately
    by each surface it drifts into meaning something different in each one,
    which is precisely how a field with two authors ends up disagreeing with
    itself. One query, one meaning, every client.
    """
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        listen_set = validate_listen_set(req.listen_set)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        rows = await inbox_unread_by_sender(
            listen_set=listen_set,
            reader_identity=req.reader_identity,
        )
        senders = [
            InboxUnreadSender(
                **{"from": r["from"], "unread": r["unread"], "latest": r["latest"]}
            )
            for r in rows
        ]
        return InboxUnreadSummaryResponse(
            status="ok",
            senders=senders,
            total=sum(s.unread for s in senders),
            guidance=(
                "Counts DIRECT unread mail only — fan-out and huddle threads are "
                "excluded, because a group message is not waiting on any one "
                "reader. This number is only truthful if your surface ACKS what "
                "it displays: render without acking and the badge climbs forever "
                "against someone the user is fully current with."
            ),
        )
    except Exception as e:
        logger.exception("inbox_unread_summary failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox/{message_id:path}/ack", response_model=InboxAckResponse)
async def ack_inbox(message_id: str, req: InboxAckRequest, request: Request):
    """Mark an inbox message as read by a specific reader."""
    # Mail lifecycle: read-gated, same rule as send — acking, archiving or
    # resolving a message addressed to you is not a write to shared memory.
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        updated = await inbox_ack(message_id=message_id, reader_identity=req.reader_identity)
        if not updated:
            raise HTTPException(status_code=404, detail=f"Inbox message {message_id!r} not found")
        return InboxAckResponse(status="ok", id=message_id, guidance=ack_guidance())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("inbox_ack failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox/{message_id:path}/archive", response_model=InboxAckResponse)
async def archive_inbox(message_id: str, req: InboxAckRequest, request: Request):
    """Archive an inbox message (hides from all inbox queries)."""
    # Mail lifecycle: read-gated, same rule as send — acking, archiving or
    # resolving a message addressed to you is not a write to shared memory.
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        updated = await inbox_archive(
            message_id=message_id,
            reader_identity=req.reader_identity,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Inbox message {message_id!r} not found")
        return InboxAckResponse(status="ok", id=message_id, guidance=archive_guidance())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("inbox_archive failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox/{message_id:path}/resolve", response_model=InboxAckResponse)
async def resolve_inbox(message_id: str, req: InboxResolveRequest, request: Request):
    """Mark an inbox message resolved so it drains from the default view.

    Unlike archive (a global hard-hide), resolve records who closed the thread
    and when, and the message stays retrievable via include_resolved=True.
    """
    # Mail lifecycle: read-gated, same rule as send — acking, archiving or
    # resolving a message addressed to you is not a write to shared memory.
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        updated = await inbox_resolve(
            message_id=message_id,
            resolver_identity=req.reader_identity,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Inbox message {message_id!r} not found")
        return InboxAckResponse(status="ok", id=message_id, guidance=resolve_guidance())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("inbox_resolve failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/inbox/resolve-thread", response_model=InboxResolveThreadResponse)
async def resolve_inbox_thread(req: InboxResolveThreadRequest, request: Request):
    """Resolve every open message in a thread that was delivered to this reader.

    For closing a room in one call. A closed huddle whose mail stays `open`
    reads as a live conversation forever — every message is present-tense and
    none of them says the room is over — and draining twenty of them one id at
    a time is a thing nobody does, so nobody drains anything.

    Idempotent: resolving an unknown or already-drained thread returns 0
    rather than an error, so a closer can call it unconditionally.
    """
    # Mail lifecycle: read-gated, same rule as send — acking, archiving or
    # resolving a message addressed to you is not a write to shared memory.
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        n = await inbox_resolve_thread(
            thread_id=req.thread_id,
            listen_set=req.listen_set,
            resolver_identity=req.reader_identity,
        )
        return InboxResolveThreadResponse(
            status="ok",
            thread_id=req.thread_id,
            resolved=n,
            guidance=(
                f"Resolved {n} message(s) in {req.thread_id}. They drain from the "
                "default inbox view and stay retrievable with include_resolved=true. "
                "Only your own copies were touched — other participants must drain "
                "their own."
            ),
        )
    except Exception as e:
        logger.exception("inbox_resolve_thread failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


# --- Inbox long-poll wait: the any-harness wake primitive -----------------

@router.post("/inbox/wait", response_model=InboxWaitResponse)
async def wait_inbox(req: InboxWaitRequest, request: Request):
    """Block until new mail arrives for the listen_set, or timeout.

    Any harness that can POST gets wake-on-message with no client binary:
    loop on this endpoint and act on what it returns. Self-echo (mail whose
    self-asserted `from` matches the reader) and `fyi` intent are excluded
    from wakes by default — same semantics as the reference watcher.
    """
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        listen_set = validate_listen_set(req.listen_set)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    started = datetime.now(timezone.utc)
    since = req.since or started
    own = {a.lower() for a in listen_set}
    if req.reader_identity:
        own.add(req.reader_identity.strip().lower())
    poll_every = 2.0
    try:
        while True:
            msgs = await inbox_list(
                listen_set=listen_set,
                reader_identity=req.reader_identity,
                unread_only=True,
                limit=50,
                newest_first=True,
            )
            fresh = [
                m for m in msgs
                if m.created_at and m.created_at > since
                and (m.from_ or "").strip().lower() not in own
                and (req.include_fyi or (m.intent or "") != "fyi")
            ]
            waited = (datetime.now(timezone.utc) - started).total_seconds()
            if fresh:
                fresh.sort(key=lambda m: m.created_at)  # oldest-first reading order
                return InboxWaitResponse(
                    status="ok", messages=fresh, waited_seconds=round(waited, 1),
                    guidance=(
                        "New mail. Ack/reply/resolve what you handle, then wait "
                        "again passing since=<newest created_at you received> as "
                        "your cursor."
                    ),
                )
            if waited >= req.timeout_seconds:
                return InboxWaitResponse(
                    status="timeout", messages=[], waited_seconds=round(waited, 1),
                    guidance="No new mail. Re-issue the wait to keep listening.",
                )
            await asyncio.sleep(min(poll_every, max(0.05, req.timeout_seconds - waited)))
    except Exception as e:
        logger.exception("inbox_wait failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


# --- Presence / liveness roster (MSG-4) ----------------------------------

@router.post("/presence", response_model=PresenceUpdateResponse)
async def update_presence(req: PresenceUpdateRequest, request: Request):
    """Self-reported liveness heartbeat: the harness POSTs its own state
    transitions (running → awaiting-input → done). Engram never scrapes."""
    # Gate on messaging MEMBERSHIP (read), matching every other protocol verb
    # — send/ack/resolve/wait all write protocol rows under the read gate.
    # Presence was the lone write-gated outlier, discovered 2026-08-16 when
    # the first fleet-read-only principal (an external assistant with no
    # fleet write BY DESIGN) could send and receive mail but not report its
    # own liveness — a participant the roster could only show as deaf.
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        if req.farewell:
            # The watcher OBSERVED the session's process exit. Checked before
            # `watcher`, because a farewell is the watcher's last act and must
            # not be mistaken for another beat — a beat would refresh the
            # clock and void the very thing being reported.
            await presence_farewell(identity=req.identity, project=req.project)
            return PresenceUpdateResponse(
                status="ok", identity=req.identity, state=req.state, collision=None
            )
        if req.watcher:
            # MSG-5/SEAT-7: a watcher beat proves an EAR is alive, which is a
            # different claim from the session's own state. It refreshes
            # liveness (presence + seat) without touching anything the session
            # reported, and never reports a collision — one watcher per
            # session is the correct arrangement, not a misconfiguration.
            await presence_watcher_beat(identity=req.identity, project=req.project)
            return PresenceUpdateResponse(
                status="ok", identity=req.identity, state=req.state, collision=None
            )
        collision = await presence_update(
            identity=req.identity,
            project=req.project,
            state=req.state,
            provider=req.provider,
            overlays=req.overlays,
            channels=req.channels,
            session_nonce=req.session_nonce,
            host=req.host,
        )
        return PresenceUpdateResponse(
            status="ok", identity=req.identity, state=req.state, collision=collision
        )
    except Exception as e:
        logger.exception("presence_update failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/roster", response_model=RosterResponse)
async def get_roster(req: RosterRequest, request: Request):
    """Who is live on a project (or #channel, or the whole box), in what state.

    Solves address discoverability: an agent asks the roster instead of
    guessing addresses, and can see whether a peer is actually staffed
    (state + is_stale) before messaging it."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        entries = await roster_list(
            project=req.project,
            channel=req.channel,
            include_done=req.include_done,
            include_expired=req.include_expired,
        )
        live = sum(1 for e in entries if not e["is_stale"])
        scope_desc = req.channel or req.project or "all projects"
        return RosterResponse(
            status="ok",
            entries=[RosterEntry(**e) for e in entries],
            guidance=(
                f"{len(entries)} known on {scope_desc} ({live} fresh, "
                f"{len(entries) - live} stale). Address an entry by its "
                f"'identity' (DM) or its 'project' (group). A stale entry's "
                f"session has stopped heartbeating — it may be dead; its state "
                f"is last-known, not current."
            ),
        )
    except Exception as e:
        logger.exception("roster_list failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
