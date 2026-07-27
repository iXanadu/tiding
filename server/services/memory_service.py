import contextlib
import hashlib
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


# --- Optimistic concurrency (MEM-4) --------------------------------------
#
# A "version" is a content hash of the stored value. Callers that do a
# read-modify-write (the motivating case: several agents each rewriting their
# own SECTION of one shared handoff document) pass the version they read as
# ``if_match``; a mismatch means someone else wrote in between and the write is
# refused rather than silently discarding their edit.
#
# Why a content hash rather than the two obvious alternatives:
#   · a TIMESTAMP cannot work — ``memory_get`` bumps ``last_used_at`` on every
#     read, so a concurrent READER would invalidate the token and produce
#     conflicts that aren't conflicts.
#   · a version COLUMN would need a schema migration; engram is public and a
#     hash needs none. It also has a better property: two writers producing
#     identical content don't conflict, because there is nothing to lose.
#
# Truncated to 16 hex chars: this guards against accidental clobbering by
# cooperating agents, not against an adversary engineering a collision.
_VERSION_LEN = 16


def compute_version(value: str | None) -> str:
    """The version token for a stored value (``''`` for a missing row)."""
    if value is None:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_VERSION_LEN]


class VersionConflict(Exception):
    """Raised when ``if_match`` doesn't match the stored value.

    Carries the CURRENT value so the caller can re-merge immediately instead
    of paying another round trip to find out what it lost the race to.
    """

    def __init__(self, current_value: str | None, current_version: str):
        self.current_value = current_value
        self.current_version = current_version
        super().__init__("version mismatch")


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
    if_match: str | None = None,
) -> tuple[str, bool, str]:
    """Store or update a memory with its embedding.

    Returns ``(key, created, version)`` — ``created=False`` means this write
    OVERWROTE an existing value; ``version`` is the content hash of what is
    now stored, for a caller that intends to read-modify-write it later.

    ``if_match`` (MEM-4) makes the write conditional: it proceeds only if the
    stored value still hashes to that version, else ``VersionConflict`` is
    raised carrying the current value. Pass ``""`` to assert the row does not
    exist yet. Omit it for today's unconditional behavior.

    Why the second element exists (MEM-1): memory identity is
    ``(namespace, key, scope, user_id, project)``, which deliberately contains
    no session dimension — the work outlives the session that wrote it. The
    consequence is that two sessions in one project writing the same key
    silently destroy each other's value, and until now both got a byte-
    identical "stored" response, so the loser could not tell it had just
    erased someone. Same shape as an unguarded bulk delete: a destructive
    outcome with no signal. The caller surfaces this so an overwrite is at
    least *visible*; it is not prevented, because overwriting your own key is
    the normal case.
    """
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
        # The if_match check and the write MUST be one transaction, with the
        # row locked — otherwise the guard has the very race it exists to
        # close (two writers both read a matching version, both proceed, one
        # is lost). SELECT ... FOR UPDATE serialises concurrent conditional
        # writers on the same key; the row is held only for this statement
        # pair, never across a client's think-time.
        async with conn.transaction() if if_match is not None else _null_ctx():
            if if_match == "":
                # MUST-NOT-EXIST is a different problem from must-match, and
                # the row lock below cannot solve it: SELECT ... FOR UPDATE has
                # nothing to lock when the row does not exist yet. Two writers
                # racing on a fresh key both read "absent", both pass, and the
                # second one's upsert takes the DO UPDATE branch straight over
                # the first's content — with both responses reporting success
                # and if_match_applied=true. AgentBeast lost real content to
                # exactly this on 2026-07-26; it survived review because every
                # test of it was sequential, where the loser reads a committed
                # row and correctly 409s.
                #
                # So absence is asserted by the UNIQUE INDEX instead of by a
                # read. DO NOTHING makes the database the arbiter: the insert
                # either wins outright or affects no row, and there is no
                # window between the check and the write because they are the
                # same statement.
                created_row = await _insert_if_absent(
                    conn, namespace, key, value, scope, user_id, project, tags,
                    tags_search, embedding, search_text, expires_at,
                    metadata_json, owner,
                )
                if created_row is None:
                    current = await conn.fetchval(
                        """
                        SELECT value FROM memories
                        WHERE namespace = $1 AND key = $2 AND scope = $3
                          AND user_id IS NOT DISTINCT FROM $4
                          AND project IS NOT DISTINCT FROM $5
                        """,
                        namespace, key, scope, user_id, project,
                    )
                    raise VersionConflict(current, compute_version(current))
                return key, True, compute_version(value)
            if if_match is not None:
                current = await conn.fetchval(
                    """
                    SELECT value FROM memories
                    WHERE namespace = $1 AND key = $2 AND scope = $3
                      AND user_id IS NOT DISTINCT FROM $4
                      AND project IS NOT DISTINCT FROM $5
                    FOR UPDATE
                    """,
                    namespace, key, scope, user_id, project,
                )
                if compute_version(current) != if_match:
                    raise VersionConflict(current, compute_version(current))
            row = await _upsert_memory(
                conn, namespace, key, value, scope, user_id, project, tags,
                tags_search, embedding, search_text, expires_at,
                metadata_json, owner,
            )
    return key, bool(row["created"]), compute_version(value)


async def _insert_if_absent(
    conn, namespace, key, value, scope, user_id, project, tags, tags_search,
    embedding, search_text, expires_at, metadata_json, owner,
):
    """Insert only if the key is unused. Returns the row, or None if taken.

    ON CONFLICT DO NOTHING is a compare-and-swap against the UNIQUE index —
    the same primitive the seat registry uses to hand out addresses without
    locks. It is the only way to assert absence race-free, because there is no
    row to lock until one exists.

    NULLS NOT DISTINCT on that index is what makes this reliable for rows with
    a NULL user_id or project: without it those rows would never collide and
    every concurrent create would "win".
    """
    return await conn.fetchrow(
        """
            INSERT INTO memories (namespace, key, value, scope, user_id, project, tags, tags_search, embedding, search_text, expires_at, metadata, owner)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
            ON CONFLICT (namespace, key, scope, user_id, project) DO NOTHING
            RETURNING id
            """,
        namespace, key, value, scope, user_id, project, tags, tags_search,
        embedding, search_text, expires_at, metadata_json, owner,
    )


@contextlib.asynccontextmanager
async def _null_ctx():
    """No-op async context — keeps the unconditional path transaction-free."""
    yield


async def _upsert_memory(
    conn, namespace, key, value, scope, user_id, project, tags, tags_search,
    embedding, search_text, expires_at, metadata_json, owner,
):
    """The upsert itself. Returns a row with a ``created`` flag.

    ``xmax = 0`` is the standard way to tell an INSERT from an upsert's UPDATE
    branch: on a freshly inserted row the deleting-transaction id is zero, on
    an updated row it carries the conflicting transaction. It costs nothing
    extra — no second query, no race window.
    """
    return await conn.fetchrow(
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
            RETURNING (xmax = 0) AS created
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
            item = dict(row)
            # MEM-4: hand back the version so a caller that intends to
            # read-modify-write this value can pass it as if_match.
            item["version"] = compute_version(item.get("value"))
            return MemoryItem(**item)
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


async def inbox_unread_by_sender(
    listen_set: list[str],
    reader_identity: str,
) -> list[dict]:
    """Per-sender count of DIRECT mail this reader has not read.

    Answers exactly one question, for a badge on a session card: "does this
    agent have something for me that I have not read yet?"

    DIRECT ONLY — group traffic is excluded on purpose, two ways:

      * rows carrying a ``participants`` set (engram's native fan-out), and
      * rows whose ``thread_id`` is a ``huddle/...`` relay thread.

    Both are excluded because "unread" does not mean one clean thing in a
    multi-party thread: a message addressed to five agents is not waiting on
    any particular one, so counting it against a single card would misreport
    a group conversation as a personal obligation.

    Why this lives on the server rather than in each client: "unread" is a
    definition, not a datum. Assembled per-surface it would quietly come to
    mean three different things — which is the shape of most of the liveness
    bugs found on 2026-07-26/27, where a field with more than one author
    disagreed with itself. One query, one meaning.

    NOTE the reader is the HUMAN here, so this count is only truthful if the
    surface displaying it ACKS what it shows. A client that renders mail
    without acking will show a monotonically climbing badge against a
    correspondent the user is fully current with — the same failure an agent
    hit at 56 unread while having read every message by another path.
    """
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set or not reader_identity:
        return []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(metadata->>'from', '(unknown)') AS sender,
                   count(*) AS unread,
                   max(created_at) AS latest
            FROM memories
            WHERE namespace = $1
              AND scope = $2
              AND user_id = ANY($3::text[])
              AND COALESCE((metadata->>'archived')::bool, false) = false
              AND COALESCE(metadata->>'status', $4) = $4
              AND NOT COALESCE(metadata->'read_by', '[]'::jsonb) ? $5::text
              -- jsonb_array_length() ERRORS on a non-array, and `participants`
              -- is absent on most rows and has been seen carrying a scalar.
              -- Type-check before measuring: a malformed row must fall through
              -- as "not a group", never take the whole query down with it.
              AND COALESCE(
                    CASE WHEN jsonb_typeof(metadata->'participants') = 'array'
                         THEN jsonb_array_length(metadata->'participants')
                         ELSE 0 END, 0) = 0
              AND COALESCE(metadata->>'thread_id', '') NOT LIKE 'huddle/%'
            GROUP BY 1
            ORDER BY max(created_at) DESC
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, listen_set, INBOX_OPEN,
            reader_identity.lower(),
        )
    return [
        {
            "from": r["sender"],
            "unread": r["unread"],
            "latest": r["latest"],
        }
        for r in rows
    ]


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
# Seat-registry constants live here, beside inbox/presence, so the heartbeat
# can refresh a seat row without importing session_registry (which imports
# THIS module — the reverse direction would be circular).
SEAT_SCOPE = "seat"
SEAT_USER_ID = "global"
PRESENCE_STALE_AFTER_SECONDS = 600  # 10 min without a heartbeat → stale


# Seat-collision detection (grew out of ROST-1): a session nonce is fresh for
# this many seconds. Window ≈ 2.5× the bridge heartbeat interval (120s) — a
# bridge restart mid-session can look like a collision for at most this long.
SEAT_COLLISION_WINDOW_SECONDS = 300
# Identities where multiple simultaneous sessions are legitimate role-sharing
# by design (per the two-axis doctrine): never flagged as collisions.
SEAT_EXEMPT_IDENTITIES = {"admin"}

# MSG-5/SEAT-7: a watcher beat is fresh for this long. Window ≈ 6.6× the
# watcher's 45s poll interval, so a slow poll or one dropped request never
# reads as a dead ear.
WATCHER_STALE_AFTER_SECONDS = 300


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
        # MSG-9: this write REPLACES metadata wholesale, so any field owned by
        # another writer must be carried forward explicitly or it is destroyed.
        #
        # `watcher_last_seen` is written by the WATCHER (presence_watcher_beat,
        # which merges with jsonb_set and so never had the reciprocal problem).
        # Because this beat rides TOOL CALLS while the watcher polls on its own
        # timer, an ACTIVE session overwrote the field far more often than the
        # watcher restored it — so `watcher_alive` read null for exactly the
        # sessions doing the most work, and a busy session advertised itself as
        # NOT LISTENING. That is the inversion MSG-5 exists to prevent, and it
        # matters because watcher liveness is the ONE death signal that does not
        # degrade when a session is head-down (SEAT-4/MSG-8).
        #
        # SEAT-4 REFINEMENT (2026-07-27, after a power outage falsified the
        # first version within four hours): carry it forward only WITHIN A
        # GENERATION. `watcher_last_seen` describes the process that armed that
        # watcher. It survives on a row the NEXT generation reclaims through
        # SEAT-9 continuity, so after a restart the dead generation's evidence
        # was being read as the live one's state and the roster called a
        # running session presumed-dead.
        #
        # The generational fact is the NONCE, not a clock. A timestamp
        # comparison ("is the watcher beat older than this beat") would work in
        # the window observed and depends on clocks being sane across a boot —
        # which is the one moment they least are, and the exact moment this
        # fires. A nonce absent from the prior map is a process we have not
        # seen before: a new generation, whose watcher has said nothing yet.
        #
        # Dropping the field yields None (NO BASIS), never False (dead) — the
        # three-valued discipline holds, and the new generation's own watcher
        # restores truth on its next poll. Only a LIVE session's beat can clear
        # it, so a genuine corpse (no beats at all) still ages to
        # presumed-dead exactly as SEAT-4 intended.
        #
        # Legacy clients send no nonce and cannot be generation-checked; they
        # keep the unconditional carry-forward, i.e. today's behaviour.
        prior_sessions = prior_md.get("sessions") or {}
        new_generation = bool(session_nonce) and session_nonce not in prior_sessions
        watcher_seen = prior_md.get("watcher_last_seen")
        if watcher_seen and not new_generation:
            metadata["watcher_last_seen"] = watcher_seen
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
        # SEAT-8: a presence heartbeat is THE liveness signal, so the seat row
        # must inherit it. Without this the seat kept its own clock — set once
        # at claim time and never refreshed — and the two disagreed about the
        # same session: observed live 2026-07-24, the roster reporting a
        # session fresh at 374s while its seat read is_live=false and
        # reclaimable=true, at which point a new session could have been
        # granted an address a running one still held.
        #
        # Server-side on purpose: the client-side refresh only reaches a
        # session once its bridge restarts, so it cannot rescue the sessions
        # already running. This one takes effect for every heartbeating
        # session the moment it deploys.
        await conn.execute(
            """
            UPDATE memories SET last_used_at = NOW()
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND project = $4 AND key = $5
            """,
            PRESENCE_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
            project, f"seat/{identity}",
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


async def presence_watcher_beat(identity: str, project: str) -> bool:
    """Record that this identity's inbox WATCHER is alive (MSG-5, SEAT-7).

    A session's liveness and its ability to HEAR are different properties. The
    bridge heartbeat rides tool calls, so it proves activity; the watcher polls
    on its own timer and lives exactly as long as the session, so it proves
    existence — and it is the only process whose presence means mail will
    actually wake somebody. A session that never armed one is fully addressable
    and permanently silent, which today is indistinguishable from "not read
    yet."

    Deliberately a NARROW write, not a second presence_update:

    * It must not join the nonce map. The watcher shares its session's
      identity, so a nonce here would read as a second live session and
      false-flag the very seat collision SEAT-3 exists to detect.
    * It must not write state/provider/overlays/channels. Those are the
      session's to report; a watcher beat carrying a default would silently
      revert an ``awaiting-input`` session to ``running``.
    * It must not INSERT. No presence row means the session has never
      heartbeated, and inventing one from a watcher would conjure a session
      that does not exist. Returns False in that case.

    It DOES refresh ``last_used_at`` on both presence and seat rows, which is
    the SEAT-7 fix: liveness stops being a proxy for tool activity, so a
    session working uninterrupted for hours no longer ages into reclaimable
    while it is alive and listening.
    """
    identity = identity.lower()
    project = project.lower()
    now = datetime.now(timezone.utc)
    key = f"presence/{identity}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        # jsonb_set merges into the existing metadata, so everything the
        # session reported survives untouched.
        updated = await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{watcher_last_seen}', to_jsonb($1::text), true
                ),
                last_used_at = NOW()
            WHERE namespace = $2 AND scope = $3 AND user_id = $4 AND key = $5
            """,
            now.isoformat(),
            PRESENCE_NAMESPACE, PRESENCE_SCOPE, project, key,
        )
        if updated == "UPDATE 0":
            return False
        await conn.execute(
            """
            UPDATE memories SET last_used_at = NOW()
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND project = $4 AND key = $5
            """,
            PRESENCE_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
            project, f"seat/{identity}",
        )
    return True


def _watcher_state(md: dict, now: datetime) -> tuple[bool | None, datetime | None]:
    """Three-valued listening state for a presence row.

    Follows the same discipline AgentBeast applies to its process-ancestry
    field, so the two sources answer with one vocabulary:

      True   a watcher beat within the freshness window — mail will wake it.
      False  a watcher HAS beaten for this identity before and has since gone
             quiet. Silence is only evidence once there was a signal to lose.
      None   no watcher has ever beaten here — no basis. An older watcher
             build, or a session that never armed one. NEVER coerce None to
             False: absent is not dead, and that conflation is what let a live
             session's address be taken in the first place.
    """
    raw = md.get("watcher_last_seen")
    if not raw:
        return None, None
    try:
        seen = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None, None
    return (now - seen).total_seconds() < WATCHER_STALE_AFTER_SECONDS, seen


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
        watcher_alive, watcher_seen = _watcher_state(md, now)
        # SEAT-4: correct the self-reported state using WATCHER truth.
        #
        # `state` is whatever the session last claimed, and a session that dies
        # never gets to retract it — so a corpse reports "running" forever.
        # That is what offered a human two dead seats to huddle with on
        # 2026-07-26, and it is why "annotate staleness, never correct it" was
        # not enough.
        #
        # The watcher is the ONLY signal licensed to override it. Its beat
        # rides its own poll timer rather than tool activity, so unlike
        # `is_stale` it does NOT degrade when a session is head-down in a long
        # call (MSG-8: a busy agent and a dead one are otherwise
        # indistinguishable). A watcher that HAS beaten and then stopped is a
        # process that exited — the positive death signal `is_stale` never had.
        #
        # watcher_alive is deliberately THREE-valued and only False overrides.
        # None means no watcher ever beat here (older build, or none armed) —
        # no basis, so the session keeps its own word. Coercing None to False
        # would declare every un-watched session dead, which is the exact
        # absent-vs-negative conflation that has caused most of this class.
        presumed_dead = watcher_alive is False and state != "done"
        if presumed_dead:
            state = "presumed-dead"
        entries.append({
            "identity": ident,
            "project": r["user_id"],
            "state": state,
            "presumed_dead": presumed_dead,
            "reported_state": md.get("state") or "running",
            "provider": md.get("provider"),
            "overlays": md.get("overlays") or [],
            "channels": md.get("channels") or [],
            "last_seen": last_seen,
            "age_seconds": round(age, 1),
            "is_stale": age >= PRESENCE_STALE_AFTER_SECONDS,
            "live_sessions": live,
            "collision": live > 1 and ident not in SEAT_EXEMPT_IDENTITIES,
            "providers_seen": sorted({(i.get("provider") or "unknown") for i in fresh.values()}) if fresh else [],
            "watcher_alive": watcher_alive,
            "watcher_last_seen": watcher_seen,
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
