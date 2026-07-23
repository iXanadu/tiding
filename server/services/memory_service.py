import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np

from server.config import settings
from server.db import get_pool
from server.embeddings import embed
from server.models import InboxMessage, MemoryItem

# NS-1: inbox/presence rows live in the primary (provider-agnostic) namespace.
INBOX_NAMESPACE = settings.primary_namespace
INBOX_SCOPE = "inbox"
INBOX_EXPIRATION_DAYS = 0  # never expire; inbox is not TTL'd
# Read-side staleness: an open message older than this is flagged "verify before
# acting" — annotated, NEVER auto-deleted (knowledge is durable; the annotation
# is the cheap, reversible variant of a coordination TTL).
INBOX_STALE_AFTER_HOURS = 72
# Lifecycle statuses. Only "open" is actionable; the rest are drained.
INBOX_OPEN = "open"
INBOX_RESOLVED = "resolved"
INBOX_SUPERSEDED = "superseded"
# Marker recorded as resolved_by when the background stale-sweep drains a
# read-but-unresolved message — distinguishes a policy drain from a human/agent
# resolve in the audit trail.
INBOX_STALE_SWEEP_ACTOR = "system:stale-sweep"

# Model composition leak: some Claude sessions emit memory_reply bodies ending
# with tool-call closing tags (e.g. "</body></invoke>") when the parameter
# value bleeds into the tool-call XML envelope. Strip any trailing combination
# of </body>, </invoke>, </parameter> before persisting.
_TOOLCALL_TRAILER_RE = re.compile(
    r"\s*(?:</body>|</invoke>|</parameter>)+\s*$",
    re.IGNORECASE,
)


def _normalize_key_fields(
    namespace: str | None = None,
    key: str | None = None,
    scope: str | None = None,
    user_id: str | None = None,
    project: str | None = None,
) -> tuple:
    """Lowercase partition key fields for case-insensitive matching."""
    return (
        namespace.lower() if namespace else namespace,
        key.lower() if key else key,
        scope.lower() if scope else scope,
        user_id.lower() if user_id else user_id,
        project.lower() if project else project,
    )


def _strip_toolcall_trailer(text: str) -> str:
    if not text:
        return text
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = _TOOLCALL_TRAILER_RE.sub("", cur)
    return cur


def _expand_key(key: str) -> str:
    """Expand snake_case/camelCase key into natural words.

    'my_location' → 'my location'
    'wifeName' → 'wife Name'
    """
    expanded = key.replace("_", " ").replace("-", " ")
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", expanded)
    return expanded.lower().strip()


def _build_search_text(key: str, value: str, tags: str) -> str:
    """Build the combined text that gets embedded and trigram-indexed."""
    key_expanded = _expand_key(key)
    parts = [key_expanded, key, value]
    if tags:
        parts.append(tags)
    return " ".join(parts)


async def memory_set(
    namespace: str,
    key: str,
    value: str,
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
    tags: str = "",
    tags_search: str = "",
    expiration_days: int = 0,
    metadata: dict | None = None,
    owner: str | None = None,
) -> str:
    """Store or update a memory with its embedding."""
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    pool = await get_pool()
    search_text = _build_search_text(key, value, tags)
    embedding = await embed(search_text)

    # 0 = never expires
    expires_at = None
    if expiration_days and expiration_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expiration_days)

    metadata_json = json.dumps(metadata) if metadata else None

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, project, tags, tags_search, embedding, search_text, expires_at, metadata, owner)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
            ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE SET
                value = EXCLUDED.value,
                tags = EXCLUDED.tags,
                tags_search = EXCLUDED.tags_search,
                embedding = EXCLUDED.embedding,
                search_text = EXCLUDED.search_text,
                expires_at = EXCLUDED.expires_at,
                metadata = EXCLUDED.metadata,
                owner = EXCLUDED.owner,
                last_used_at = NOW()
            """,
            namespace,
            key,
            value,
            scope,
            user_id,
            project,
            tags,
            tags_search,
            embedding,
            search_text,
            expires_at,
            metadata_json,
            owner,
        )
    return key


async def memory_get(
    namespace: str,
    key: str,
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
) -> MemoryItem | None:
    """Retrieve a memory by exact key within a namespace."""
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT namespace, key, value, scope, user_id, project, tags, tags_search, created_at
            FROM memories
            WHERE namespace = $1 AND key = $2 AND scope = $3
              AND user_id IS NOT DISTINCT FROM $4
              AND project IS NOT DISTINCT FROM $5
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            namespace,
            key,
            scope,
            user_id,
            project,
        )
        if row:
            await conn.execute(
                """UPDATE memories SET last_used_at = NOW()
                   WHERE namespace = $1 AND key = $2 AND scope = $3
                     AND user_id IS NOT DISTINCT FROM $4
                     AND project IS NOT DISTINCT FROM $5""",
                namespace,
                key,
                scope,
                user_id,
                project,
            )
            return MemoryItem(**dict(row))
    return None


async def memory_search(
    namespaces: list[str],
    query: str,
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
    limit: int = 5,
) -> list[MemoryItem]:
    """Hybrid vector + trigram search, scoped to namespace(s) and user."""
    _, _, scope, user_id, project = _normalize_key_fields(
        scope=scope, user_id=user_id, project=project
    )
    namespaces = [ns.lower() for ns in namespaces]
    pool = await get_pool()
    query_embedding = await embed(query)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH vector_results AS (
                SELECT
                    namespace, key, value, scope, user_id, project, tags, tags_search,
                    created_at,
                    1 - (embedding <=> $1) AS vec_score,
                    similarity(search_text, $2) AS trgm_score
                FROM memories
                WHERE (expires_at IS NULL OR expires_at > NOW())
                  AND namespace = ANY($3::text[])
                  AND scope = $4
                  AND scope <> 'inbox'
                  AND user_id IS NOT DISTINCT FROM $5
                  AND project IS NOT DISTINCT FROM $6
                ORDER BY embedding <=> $1
                LIMIT $7 * 3
            )
            SELECT *,
                   vec_score + ($8 * trgm_score) AS combined_score
            FROM vector_results
            WHERE vec_score >= $9 OR trgm_score >= $10
            ORDER BY combined_score DESC
            LIMIT $7
            """,
            query_embedding,
            query,
            namespaces,
            scope,
            user_id,
            project,
            limit,
            settings.trigram_weight,
            settings.vector_threshold,
            settings.trigram_threshold,
        )

        results = []
        keys_to_update: dict[str, list[str]] = {}
        for row in rows:
            results.append(
                MemoryItem(
                    namespace=row["namespace"],
                    key=row["key"],
                    value=row["value"],
                    scope=row["scope"],
                    user_id=row["user_id"],
                    project=row["project"],
                    tags=row["tags"],
                    tags_search=row["tags_search"],
                    score=round(float(row["combined_score"]), 4),
                    created_at=row["created_at"],
                )
            )
            keys_to_update.setdefault(row["namespace"], []).append(row["key"])

        for ns, keys in keys_to_update.items():
            await conn.execute(
                """UPDATE memories SET last_used_at = NOW()
                   WHERE namespace = $1 AND key = ANY($2) AND scope = $3
                     AND user_id IS NOT DISTINCT FROM $4
                     AND project IS NOT DISTINCT FROM $5""",
                ns,
                keys,
                scope,
                user_id,
                project,
            )

    return results


async def memory_forget(
    namespace: str,
    key: str,
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
) -> bool:
    """Delete a memory by key within a namespace. Returns True if found and deleted."""
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """DELETE FROM memories
               WHERE namespace = $1 AND key = $2 AND scope = $3
                 AND user_id IS NOT DISTINCT FROM $4
                 AND project IS NOT DISTINCT FROM $5""",
            namespace,
            key,
            scope,
            user_id,
            project,
        )
    return result == "DELETE 1"


# --- Inbox operations ----------------------------------------------------
#
# Inbox messages are ordinary rows in the `memories` table with:
#   namespace = 'claude-code'
#   scope     = 'inbox'
#   user_id   = <to address>   — e.g. 'engram', 'machine:macmini'
#   key       = 'inbox/<uuid>' — unique per message
#   metadata  = {kind: 'inbox', from, subject, thread_id, read_by[], archived}
#
# This reuses the existing table, indexes, and auth — no schema migration.

def _row_to_inbox_message(row: dict) -> InboxMessage:
    md = row.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    created_at = row["created_at"]
    age_hours = None
    is_stale = False
    if created_at is not None:
        delta = datetime.now(timezone.utc) - created_at
        age_hours = round(delta.total_seconds() / 3600.0, 1)
        is_stale = age_hours >= INBOX_STALE_AFTER_HOURS
    return InboxMessage(
        id=row["key"],
        to=row["user_id"],
        from_=md.get("from"),
        from_principal=md.get("from_principal"),
        authority=bool(md.get("authority", False)),
        intent=md.get("intent"),
        subject=md.get("subject", ""),
        body=row["value"],
        thread_id=md.get("thread_id"),
        participants=md.get("participants") or [],
        read_by=md.get("read_by", []) or [],
        archived=bool(md.get("archived", False)),
        created_at=created_at,
        status=md.get("status") or INBOX_OPEN,
        resolved_by=md.get("resolved_by"),
        resolved_at=md.get("resolved_at"),
        supersedes=md.get("supersedes"),
        superseded_by=md.get("superseded_by"),
        is_stale=is_stale,
        age_hours=age_hours,
    )


async def inbox_send(
    to: str,
    body: str,
    subject: str = "",
    from_: str | None = None,
    thread_id: str | None = None,
    supersedes: str | None = None,
    from_principal: str | None = None,
    authority: bool = False,
    intent: str | None = None,
    participants: list[str] | None = None,
) -> str:
    """Create an inbox message. Returns the generated message id (memory key).

    When ``supersedes`` names a prior message, that message is marked
    ``superseded`` (with ``superseded_by`` pointing here) so the stale one drops
    out of the default inbox view — the sender knows when they're revising.
    """
    to = to.lower()
    body = _strip_toolcall_trailer(body)
    subject = _strip_toolcall_trailer(subject)
    msg_id = f"inbox/{uuid.uuid4()}"
    metadata = {
        "kind": "inbox",
        "from": from_,
        # Server-derived provenance (never client-settable): the authenticated
        # principal that actually sent this, and whether it is an owner. `from`
        # above is the self-asserted label; these are the verified truth the
        # render layer distinguishes (MSG-1/MSG-2). A worker holding the shared
        # project token cannot forge `authority` — only an owner principal can.
        "from_principal": from_principal,
        "authority": bool(authority),
        "intent": intent,
        "subject": subject,
        "thread_id": thread_id,
        "read_by": [],
        "archived": False,
        "status": INBOX_OPEN,
        "supersedes": supersedes,
        # HUD-1: the full membership of a fan-out conversation, so a reply can
        # reach the whole group rather than only the sender. Absent (None) on
        # ordinary 1:1 mail — the presence of this key is what marks a thread
        # as multi-party, so it must NOT be defaulted to an empty list here.
        "participants": participants or None,
    }
    # Minimal embedding — we never semantic-search inbox, but the column is NOT NULL.
    search_text = f"{subject} {body}"
    embedding = await embed(search_text)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO memories
                    (namespace, key, value, scope, user_id, tags, tags_search,
                     embedding, search_text, expires_at, metadata)
                VALUES ($1, $2, $3, $4, $5, '', '', $6, $7, NULL, $8::jsonb)
                """,
                INBOX_NAMESPACE,
                msg_id,
                body,
                INBOX_SCOPE,
                to,
                embedding,
                search_text,
                json.dumps(metadata),
            )
            if supersedes:
                await conn.execute(
                    """
                    UPDATE memories
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'status', $1::text,
                        'superseded_by', $2::text
                    )
                    WHERE namespace = $3 AND scope = $4 AND key = $5
                    """,
                    INBOX_SUPERSEDED,
                    msg_id,
                    INBOX_NAMESPACE,
                    INBOX_SCOPE,
                    supersedes,
                )
    return msg_id


async def inbox_list(
    listen_set: list[str],
    reader_identity: str | None = None,
    unread_only: bool = True,
    limit: int = 20,
    include_resolved: bool = False,
    newest_first: bool = False,
) -> list[InboxMessage]:
    """List inbox messages addressed to any member of ``listen_set``.

    When ``unread_only`` is True and ``reader_identity`` is given, messages
    already read by that reader are filtered out.

    By default only ``open`` messages are returned — resolved and superseded
    mail has drained and must not wake or trip a fresh session. A NULL/missing
    status is treated as ``open`` (back-compat with pre-lifecycle messages).
    Set ``include_resolved=True`` to see the full history.

    ``newest_first`` flips the sort to ``created_at DESC``. Display callers
    (memory_inbox) want oldest-first reading order and leave this False. The
    ``--follow`` watcher sets it True: it never acks, so its unread set grows
    unbounded, and an oldest-first ``LIMIT`` would truncate genuinely-new mail
    out of the window once the backlog exceeds ``limit`` — the watcher would go
    blind exactly when the inbox is busy. Newest-first keeps new arrivals in
    the window regardless of backlog size (its ``seen`` set dedups the rest).
    """
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set:
        return []
    # Always select the NEWEST `limit` messages (inner DESC LIMIT), then present
    # them in the caller's reading order (outer sort). Selecting the oldest N
    # would hide the most-recent mail once the backlog exceeds `limit` — wrong
    # for an inbox, where newest is most relevant (a small limit made the newest
    # messages invisible; caught by agentbeast 2026-07-19). Display callers get
    # the newest N oldest-first for reading; the watcher (newest_first) keeps
    # newest-first.
    display_order = "DESC" if newest_first else "ASC"
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT key, value, user_id, metadata, created_at FROM (
                SELECT key, value, user_id, metadata, created_at
                FROM memories
                WHERE namespace = $1
                  AND scope = $2
                  AND user_id = ANY($3::text[])
                  AND COALESCE((metadata->>'archived')::bool, false) = false
                  AND (
                      $7::bool
                      OR COALESCE(metadata->>'status', $8) = $8
                  )
                  AND (
                      NOT $4::bool
                      OR $5::text IS NULL
                      OR NOT COALESCE(metadata->'read_by', '[]'::jsonb) ? $5::text
                  )
                ORDER BY created_at DESC
                LIMIT $6
            ) recent
            ORDER BY created_at {display_order}
            """,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            listen_set,
            unread_only,
            reader_identity,
            limit,
            include_resolved,
            INBOX_OPEN,
        )
    return [_row_to_inbox_message(dict(r)) for r in rows]


# --- Presence / liveness roster (MSG-4) ----------------------------------
#
# Presence rows reuse the memories table exactly as inbox does — zero schema
# migration:
#   namespace = 'claude-code'          (PRESENCE_NAMESPACE)
#   scope     = 'presence'
#   user_id   = <bare project name>    — the roster grouping key
#   key       = 'presence/<identity>'  — one row per live identity (upserted)
#   metadata  = {provider, state, overlays, channels, last_seen}
#
# State is SELF-REPORTED by the harness (the worker POSTs its transitions);
# engram never scrapes. last_seen staleness is the only server-side signal:
# a session that dies mid-run stops heartbeating and goes stale.

PRESENCE_NAMESPACE = INBOX_NAMESPACE
PRESENCE_SCOPE = "presence"
PRESENCE_STALE_AFTER_SECONDS = 600  # 10 min without a heartbeat → stale


# Seat-collision detection (grew out of ROST-1): a session nonce is fresh for
# this many seconds. Window ≈ 2.5× the bridge heartbeat interval (120s) — a
# bridge restart mid-session can look like a collision for at most this long.
SEAT_COLLISION_WINDOW_SECONDS = 300
# Identities where multiple simultaneous sessions are legitimate role-sharing
# by design (per the two-axis doctrine): never flagged as collisions.
SEAT_EXEMPT_IDENTITIES = {"admin"}


def _fresh_sessions(md: dict, now: datetime) -> dict:
    """Return the still-fresh entries of a presence row's nonce map."""
    fresh = {}
    for nonce, info in (md.get("sessions") or {}).items():
        try:
            seen = datetime.fromisoformat(info.get("last_seen"))
        except (TypeError, ValueError):
            continue
        if (now - seen).total_seconds() < SEAT_COLLISION_WINDOW_SECONDS:
            fresh[nonce] = info
    return fresh


async def presence_update(
    identity: str,
    project: str,
    state: str,
    provider: str | None = None,
    overlays: list[str] | None = None,
    channels: list[str] | None = None,
    session_nonce: str | None = None,
) -> dict | None:
    """Upsert this identity's presence row (self-reported heartbeat).

    Returns a collision dict ({"live_sessions": n, "providers": [...]}) when
    more than one live session (distinct fresh nonces) is heartbeating this
    identity and the identity is not an exempt shared role — the "two bodies,
    one seat" misconfiguration. None otherwise.

    First insert embeds a small search text (column is NOT NULL); subsequent
    heartbeats update only value/metadata — no re-embedding on every beat.
    """
    identity = identity.lower()
    project = project.lower()
    now = datetime.now(timezone.utc)
    key = f"presence/{identity}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        prior = await conn.fetchrow(
            """
            SELECT metadata FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3 AND key = $4
            """,
            PRESENCE_NAMESPACE, PRESENCE_SCOPE, project, key,
        )
        prior_md = {}
        if prior and prior["metadata"]:
            prior_md = prior["metadata"]
            if isinstance(prior_md, str):
                prior_md = json.loads(prior_md)
        # Merge this beat into the pruned nonce map. Legacy clients (no nonce)
        # don't participate in collision tracking but keep normal presence.
        sessions = _fresh_sessions(prior_md, now)
        if session_nonce:
            sessions[session_nonce] = {
                "last_seen": now.isoformat(),
                "provider": provider,
                "state": state,
            }
        metadata = {
            "kind": "presence",
            "provider": provider,
            "state": state,
            "overlays": overlays or [],
            "channels": channels or [],
            "last_seen": now.isoformat(),
            "sessions": sessions,
        }
        value = f"{identity} [{provider or 'unknown'}] {state} on {project}"
        # Heartbeat timestamp rides last_used_at (no updated_at column).
        updated = await conn.execute(
            """
            UPDATE memories
            SET value = $1, metadata = $2::jsonb, last_used_at = NOW()
            WHERE namespace = $3 AND scope = $4 AND user_id = $5 AND key = $6
            """,
            value, json.dumps(metadata),
            PRESENCE_NAMESPACE, PRESENCE_SCOPE, project, key,
        )
        if updated == "UPDATE 0":
            embedding = await embed(value)
            await conn.execute(
                """
                INSERT INTO memories
                    (namespace, key, value, scope, user_id, tags, tags_search,
                     embedding, search_text, expires_at, metadata)
                VALUES ($1, $2, $3, $4, $5, '', '', $6, $7, NULL, $8::jsonb)
                ON CONFLICT (namespace, key, scope, user_id, project) DO UPDATE
                    SET value = EXCLUDED.value, metadata = EXCLUDED.metadata,
                        last_used_at = NOW()
                """,
                PRESENCE_NAMESPACE, key, value, PRESENCE_SCOPE, project,
                embedding, value, json.dumps(metadata),
            )
    if len(sessions) > 1 and identity not in SEAT_EXEMPT_IDENTITIES:
        providers = sorted({(i.get("provider") or "unknown") for i in sessions.values()})
        return {"live_sessions": len(sessions), "providers": providers}
    return None


async def roster_list(
    project: str | None = None,
    channel: str | None = None,
    include_done: bool = False,
) -> list[dict]:
    """Who is on a project (or channel, or the whole box) and in what state.

    Returns entry dicts sorted freshest-first. Staleness is annotated, never
    deleted — a stale 'running' entry means the session likely died mid-run.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, value, user_id, metadata, last_used_at
            FROM memories
            WHERE namespace = $1 AND scope = $2
              AND ($3::text IS NULL OR user_id = $3)
            ORDER BY last_used_at DESC
            """,
            PRESENCE_NAMESPACE, PRESENCE_SCOPE,
            project.lower() if project else None,
        )
    now = datetime.now(timezone.utc)
    entries = []
    for r in rows:
        md = r["metadata"] or {}
        if isinstance(md, str):
            md = json.loads(md)
        state = md.get("state") or "running"
        if state == "done" and not include_done:
            continue
        if channel and channel.lower() not in [c.lower() for c in md.get("channels") or []]:
            continue
        last_seen = r["last_used_at"] or now
        age = (now - last_seen).total_seconds()
        ident = r["key"].removeprefix("presence/")
        fresh = _fresh_sessions(md, now)
        live = max(len(fresh), 1)
        entries.append({
            "identity": ident,
            "project": r["user_id"],
            "state": state,
            "provider": md.get("provider"),
            "overlays": md.get("overlays") or [],
            "channels": md.get("channels") or [],
            "last_seen": last_seen,
            "age_seconds": round(age, 1),
            "is_stale": age >= PRESENCE_STALE_AFTER_SECONDS,
            "live_sessions": live,
            "collision": live > 1 and ident not in SEAT_EXEMPT_IDENTITIES,
            "providers_seen": sorted({(i.get("provider") or "unknown") for i in fresh.values()}) if fresh else [],
        })
    return entries


async def inbox_banner(
    listen_set: list[str],
    reader_identity: str | None,
    preview_limit: int = 5,
) -> dict | None:
    """Return ``{unread_count, preview}`` if there are unread messages, else None."""
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set:
        return None
    msgs = await inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=True,
        limit=preview_limit + 1,
    )
    if not msgs:
        return None
    preview = []
    for m in msgs[:preview_limit]:
        sender = m.from_ or "unknown"
        subject = m.subject or (m.body[:60] + ("…" if len(m.body) > 60 else ""))
        stale = f" ⚠️ STALE ({int(m.age_hours // 24)}d — verify)" if m.is_stale else ""
        preview.append(f"{sender} → {m.to}: {subject}{stale}")
    return {"unread_count": len(msgs), "preview": preview}


async def inbox_ack(message_id: str, reader_identity: str) -> bool:
    """Append ``reader_identity`` to ``metadata.read_by`` for the given inbox message.

    Idempotent — re-acking is a no-op. Returns True if the message exists.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(
                COALESCE(metadata, '{}'::jsonb),
                '{read_by}',
                CASE
                    WHEN metadata->'read_by' IS NULL THEN jsonb_build_array($1::text)
                    WHEN metadata->'read_by' ? $1::text THEN metadata->'read_by'
                    ELSE metadata->'read_by' || to_jsonb($1::text)
                END
            )
            WHERE namespace = $2 AND scope = $3 AND key = $4
            """,
            reader_identity,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            message_id,
        )
    return result.endswith(" 1")


async def inbox_archive(message_id: str, reader_identity: str | None = None) -> bool:
    """Mark an inbox message archived. Also acks for the reader if given."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE memories
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{archived}',
                    'true'::jsonb
                )
                WHERE namespace = $1 AND scope = $2 AND key = $3
                """,
                INBOX_NAMESPACE,
                INBOX_SCOPE,
                message_id,
            )
            if reader_identity and result.endswith(" 1"):
                await conn.execute(
                    """
                    UPDATE memories
                    SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{read_by}',
                        CASE
                            WHEN metadata->'read_by' IS NULL THEN jsonb_build_array($1::text)
                            WHEN metadata->'read_by' ? $1::text THEN metadata->'read_by'
                            ELSE metadata->'read_by' || to_jsonb($1::text)
                        END
                    )
                    WHERE namespace = $2 AND scope = $3 AND key = $4
                    """,
                    reader_identity,
                    INBOX_NAMESPACE,
                    INBOX_SCOPE,
                    message_id,
                )
    return result.endswith(" 1")


async def inbox_resolve(message_id: str, resolver_identity: str | None = None) -> bool:
    """Mark an inbox message ``resolved`` so it drains from the default view.

    Records who resolved it and when. Also acks for the resolver (resolving
    implies you've handled it). Either party in a thread may resolve. Returns
    True if the message exists.
    """
    resolved_at = datetime.now(timezone.utc).isoformat()
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories
            SET metadata = COALESCE(metadata, '{}'::jsonb)
                || jsonb_build_object('status', $1::text, 'resolved_at', $2::text)
                || CASE WHEN $3::text IS NULL THEN '{}'::jsonb
                        ELSE jsonb_build_object('resolved_by', $3::text) END
                || CASE
                       WHEN $3::text IS NULL THEN '{}'::jsonb
                       WHEN COALESCE(metadata->'read_by', '[]'::jsonb) ? $3::text
                           THEN '{}'::jsonb
                       ELSE jsonb_build_object(
                           'read_by',
                           COALESCE(metadata->'read_by', '[]'::jsonb) || to_jsonb($3::text)
                       )
                   END
            WHERE namespace = $4 AND scope = $5 AND key = $6
            """,
            INBOX_RESOLVED,
            resolved_at,
            resolver_identity,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            message_id,
        )
    return result.endswith(" 1")


async def inbox_autoresolve_stale(
    older_than_hours: int = INBOX_STALE_AFTER_HOURS,
    resolver: str = INBOX_STALE_SWEEP_ACTOR,
    batch_size: int = 1000,
) -> int:
    """Auto-resolve open inbox messages that a recipient has READ and that are
    older than ``older_than_hours``. Returns the number resolved.

    This drains the read-but-never-resolved tail that otherwise accumulates
    without bound: resolve is manual and optional, one-way FYIs never get a
    reply to close them, and dormant recipients never return to resolve their
    own mail — so relying on per-agent discipline leaves the pile growing.

    Safe by construction. It only touches messages that are ALREADY read (so it
    never hides undelivered mail — the "not going through" case), are past the
    staleness threshold, and are not archived. Resolve is reversible and stays
    retrievable via ``include_resolved`` — nothing is deleted. The ``resolver``
    marker (``system:stale-sweep``) keeps policy drains distinct from human ones.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memories
            SET metadata = COALESCE(metadata, '{}'::jsonb)
                || jsonb_build_object(
                       'status', $1::text,
                       'resolved_at', to_jsonb(now()),
                       'resolved_by', $2::text
                   )
            WHERE key IN (
                SELECT key FROM memories
                WHERE namespace = $3 AND scope = $4
                  AND COALESCE(metadata->>'status', $5) = $5
                  AND COALESCE((metadata->>'archived')::bool, false) = false
                  AND jsonb_array_length(COALESCE(metadata->'read_by', '[]'::jsonb)) > 0
                  AND created_at < now() - make_interval(hours => $6)
                ORDER BY created_at ASC
                LIMIT $7
            )
              AND namespace = $3 AND scope = $4
            """,
            INBOX_RESOLVED,
            resolver,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            INBOX_OPEN,
            older_than_hours,
            batch_size,
        )
    # asyncpg returns a command tag like "UPDATE 138"
    return int(result.split()[-1]) if result.startswith("UPDATE") else 0


async def inbox_counts(
    listen_set: list[str],
    reader_identity: str | None = None,
) -> dict:
    """Return a status digest for a listen_set: ``{open, resolved, superseded,
    stale}``. ``stale`` is the subset of ``open`` past the staleness threshold.
    Resolved/superseded counts are reassurance ("handled, not lost") for the
    inbox digest. NULL/missing status counts as ``open``.
    """
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set:
        return {"open": 0, "resolved": 0, "superseded": 0, "stale": 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = $4) AS open,
              COUNT(*) FILTER (WHERE status = $5) AS resolved,
              COUNT(*) FILTER (WHERE status = $6) AS superseded,
              COUNT(*) FILTER (
                  WHERE status = $4
                    AND created_at < (now() - ($7::int * interval '1 hour'))
              ) AS stale
            FROM (
                SELECT COALESCE(metadata->>'status', $4) AS status, created_at
                FROM memories
                WHERE namespace = $1
                  AND scope = $2
                  AND user_id = ANY($3::text[])
                  AND COALESCE((metadata->>'archived')::bool, false) = false
            ) s
            """,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            listen_set,
            INBOX_OPEN,
            INBOX_RESOLVED,
            INBOX_SUPERSEDED,
            INBOX_STALE_AFTER_HOURS,
        )
    return {
        "open": row["open"],
        "resolved": row["resolved"],
        "superseded": row["superseded"],
        "stale": row["stale"],
    }
