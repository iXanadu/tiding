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
    InboxAckRequest,
    InboxAckResponse,
    InboxBanner,
    InboxListRequest,
    InboxListResponse,
    InboxResolveRequest,
    InboxSendRequest,
    InboxSendResponse,
    InboxWaitRequest,
    InboxWaitResponse,
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryGetRequest,
    MemoryGetResponse,
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
from server.services.identity import autocorrect_address, validate_listen_set
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
    inbox_ack,
    inbox_archive,
    inbox_banner,
    inbox_counts,
    inbox_list,
    inbox_resolve,
    inbox_send,
    memory_forget,
    memory_get,
    memory_search,
    memory_set,
    presence_update,
    roster_list,
)

logger = logging.getLogger(__name__)

# Inbox and presence rows have their own lifecycle endpoints (send/ack/resolve/
# archive; presence heartbeat). The generic set/forget path must NOT reach them
# — otherwise a writer could overwrite a message body (wiping read_by /
# from_principal) or delete mail outside its lifecycle.
_RESERVED_SCOPES = {INBOX_SCOPE, PRESENCE_SCOPE}


def _reject_reserved_scope(scope: str | None) -> None:
    if scope in _RESERVED_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope '{scope}' is managed by its own endpoints "
                   f"(inbox: /memory/send, presence: /memory/presence) — "
                   f"not writable via /memory/set or /memory/forget.",
        )

router = APIRouter(prefix="/memory", tags=["memory"])


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
    owner = principal["name"] if principal else None
    try:
        key = await memory_set(
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
        )
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
        return MemorySetResponse(
            status="ok", key=key, namespace=req.namespace, inbox_banner=banner
        )
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
        return MemoryGetResponse(status="not_found")
    except Exception as e:
        logger.exception("memory_get failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


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
        )
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


@router.post("/forget", response_model=MemoryForgetResponse)
async def forget_memory(req: MemoryForgetRequest, request: Request):
    logger.debug(f"FORGET ns={req.namespace} key={req.key} scope={req.scope} user_id={req.user_id}")
    _reject_reserved_scope(req.scope)
    check_namespace_access(get_current_principal(request), req.namespace, "write")
    try:
        deleted = await memory_forget(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
        )
        status = "ok" if deleted else "not_found"
        return MemoryForgetResponse(status=status, key=req.key)
    except Exception as e:
        logger.exception("memory_forget failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


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
    check_namespace_access(principal, INBOX_NAMESPACE, "write")
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
    if is_fanout:
        participants = _participant_set(
            [t for t, _ in corrected], sender=req.from_
        )
        thread_id = thread_id or f"inbox/{uuid.uuid4()}"
    try:
        ids: list[str] = []
        for to, _orig in corrected:
            msg_id = await inbox_send(
                to=to,
                body=req.body,
                subject=req.subject,
                from_=req.from_,
                thread_id=thread_id,
                participants=participants,
                supersedes=req.supersedes if not ids else None,  # supersede once
                intent=req.intent,
                # Server-derived, unspoofable: taken from the authenticated principal
                # (request.state), NOT the request body — a client cannot assert
                # someone else's identity or forge owner authority (MSG-1/MSG-2).
                from_principal=(principal or {}).get("name"),
                authority=bool(principal and principal.get("is_admin")),
            )
            ids.append(msg_id)
        first_to, first_corrected = corrected[0]
        if len(corrected) == 1:
            guidance = send_guidance(to=first_to, reader_identity=req.from_)
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
        return InboxSendResponse(
            status="ok",
            id=ids[0],
            ids=ids if len(ids) > 1 else None,
            corrected_from=first_corrected,
            guidance=guidance,
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


@router.post("/inbox/{message_id:path}/ack", response_model=InboxAckResponse)
async def ack_inbox(message_id: str, req: InboxAckRequest, request: Request):
    """Mark an inbox message as read by a specific reader."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
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
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
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
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
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
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
    try:
        collision = await presence_update(
            identity=req.identity,
            project=req.project,
            state=req.state,
            provider=req.provider,
            overlays=req.overlays,
            channels=req.channels,
            session_nonce=req.session_nonce,
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
