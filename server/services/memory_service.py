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


class OwnershipConflict(Exception):
    """Raised when a principal tries to overwrite a row another one wrote.

    The owner's rule (2026-08-05): a development agent may READ any project
    memory in the fleet, but may not CHANGE one another agent wrote — shared
    scope excepted, because shared is deliberately everyone's.

    This is enforceable only because ``owner`` is recorded SERVER-SIDE from the
    authenticated token and is therefore authoritative, unlike ``user_id``,
    which the client supplies and can simply assert. Measured 2026-08-05: one
    agent wrote a project row claiming a peer's ``user_id``, the server
    returned 200, and the peer's value was gone — while ``owner`` on that same
    row correctly recorded the real writer the whole time. The data to stop it
    was already being collected; nothing consulted it.

    Carries both names so the refusal names who holds the row rather than
    saying "denied" — a caller that cannot see the holder cannot tell a
    permission problem from a partition mistake.
    """

    def __init__(self, current_owner: str, attempted_by: str | None):
        self.current_owner = current_owner
        self.attempted_by = attempted_by
        super().__init__(
            f"owned by {current_owner!r}, not {attempted_by!r}"
        )


class ForgetDenied(Exception):
    """Raised when a principal tries to hard-delete a row it does not control.

    MEM-8 (2026-08-16): destruction is self-only. The controller of a row is
    its ``custodian`` when set (estate transfer), otherwise its ``owner`` (the
    original author — immutable). Anyone with namespace write may still
    supersede or flag the row for deletion; only the controller or an admin
    may make it physically cease to exist, because deletion is the one verb
    whose damage only backups can undo. Before this gate, any fleet writer
    could hard-delete any other writer's rows — the namespace wall was the
    only check, and every dev agent shares the namespace.

    Carries the controller's name so the refusal is diagnosable, same
    doctrine as OwnershipConflict.
    """

    def __init__(self, controller: str, attempted_by: str | None):
        self.controller = controller
        self.attempted_by = attempted_by
        super().__init__(
            f"controlled by {controller!r}, not {attempted_by!r}"
        )


# MEM-8: lifecycle statuses that drain a row from default reads. 'superseded'
# is retirement (MEM-3); 'deletion_requested' additionally queues the row for
# physical purge by an admin/librarian — hidden IMMEDIATELY so a flagged
# secret's exposure window closes at flag time, not at sweep time.
HIDDEN_STATUSES = ("superseded", "deletion_requested")


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
    actor_is_admin: bool = False,
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

    # OWN-1. Ownership is enforced for scope=project ONLY, which is exactly the
    # rule as stated: project memory belongs to its writer, shared is
    # everyone's. Deliberately NOT extended to the protocol scopes (inbox,
    # presence, seat) — mail and registry rows are written and amended by
    # parties other than their author BY DESIGN (acks, resolves, releases), so
    # an ownership gate there would break messaging fleet-wide to fix a problem
    # those scopes do not have.
    enforce_owner = scope == "project" and not actor_is_admin

    async with pool.acquire() as conn:
        # The if_match check and the write MUST be one transaction, with the
        # row locked — otherwise the guard has the very race it exists to
        # close (two writers both read a matching version, both proceed, one
        # is lost). SELECT ... FOR UPDATE serialises concurrent conditional
        # writers on the same key; the row is held only for this statement
        # pair, never across a client's think-time.
        async with (
            conn.transaction()
            if (if_match is not None or enforce_owner)
            else _null_ctx()
        ):
            if enforce_owner:
                # FOR UPDATE, and inside the transaction, for the same reason
                # if_match is: a check that releases its row before the write
                # has the exact race it exists to close — two writers both read
                # "owned by me", both proceed, one row survives.
                existing_owner = await conn.fetchval(
                    """
                    SELECT owner FROM memories
                    WHERE namespace = $1 AND key = $2 AND scope = $3
                      AND user_id IS NOT DISTINCT FROM $4
                      AND project IS NOT DISTINCT FROM $5
                    FOR UPDATE
                    """,
                    namespace, key, scope, user_id, project,
                )
                # A NULL owner is a row that predates the column (12,525 of
                # them at the time of writing). It is allowed through rather
                # than locked away: refusing would make the legacy corpus
                # permanently unwritable by anyone, to protect authorship
                # nobody recorded. The upsert stamps `owner` on the way past,
                # so the corpus becomes protected as it is touched instead of
                # needing a migration.
                if existing_owner is not None and existing_owner != owner:
                    raise OwnershipConflict(existing_owner, owner)
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
        if scope == "project":
            # MEM-6: a project write SUPERSEDES any other writer's live row on
            # the same logical key. Project memory belongs to the PROJECT
            # (owner directive 2026-08-13); the writer principal is provenance,
            # not a partition gate — but the partition is physically real
            # (UNIQUE includes user_id), so without this, a second provider
            # writing `startup/next` creates a TWIN, not an update, and both
            # rows rank in search with nothing marking which is current.
            # Measured that day: seven projects split, five of them on
            # exactly the handoff keys.
            #
            # Server-side deliberately: every client inherits it — HA, grok,
            # codex, cursor — not just the CC bridge. Reuses the ec6518a
            # supersede lifecycle verbatim: the twin is KEPT, value and writer
            # untouched, retrievable via include_superseded; it merely drains
            # from default reads. Nothing is deleted, so provenance survives
            # by construction, and a write RACE between two providers resolves
            # by ordering — the later write's stamp wins — which is the same
            # latest-wins semantics single-writer upserts already have.
            #
            # Outside the if_match/owner transaction on purpose: the upsert is
            # already committed and correct on its own; a crash between it and
            # this stamp leaves only the pre-MEM-6 state (a visible twin),
            # never a lost write.
            stamp = {
                "status": "superseded",
                "superseded_at": datetime.now(timezone.utc).isoformat(),
                "superseded_by_principal": owner,
                "superseded_by_user_id": user_id,
                "superseded_reason": (
                    "a newer write of this project key by another writer "
                    "(MEM-6 cross-writer collapse)"
                ),
                "superseded_by_key": key,
            }
            await conn.execute(
                """
                UPDATE memories
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $6::jsonb
                WHERE namespace = $1 AND key = $2 AND scope = $3
                  AND project IS NOT DISTINCT FROM $4
                  AND user_id IS DISTINCT FROM $5
                  AND COALESCE(metadata->>'status', '') NOT IN ('superseded', 'deletion_requested')
                """,
                namespace, key, scope, project, user_id, json.dumps(stamp),
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
    """Retrieve a memory by exact key within a namespace.

    ``user_id="*"`` collapses across writers — honored ONLY for
    ``scope=project``, under exactly the MEM-5 search rule: the ``project``
    column already does the scoping, permission is enforced at the NAMESPACE
    level, and any writer's row was always readable by naming that writer
    explicitly, so the wildcard grants nothing new. For every other scope the
    wildcard is treated as a literal (there ``user_id`` is a person or a
    host, and spanning it would be a disclosure).

    Why GET needs this when search already had it (MEM-6, measured
    2026-08-13): exact-key reads are how handoffs work — ``startup/next``,
    ``wip/current`` — and an exact read of the caller's own partition
    silently misses a peer provider's row. Seven projects held split memory,
    five of them on exactly those keys: two providers handing off to
    themselves in parallel, neither able to see the other.

    Collapse rule: superseded rows are skipped (they drained from default
    reads by design); among live rows, newest ``created_at`` wins. With the
    write path auto-superseding cross-writer twins, at most one live row
    exists per logical key going forward — the tiebreak only ever decides
    among LEGACY twins, and that ambiguity retires organically as keys are
    rewritten. (``created_at``/``last_used_at`` deliberately do NOT rank
    authority in general: the upsert updates value in place without touching
    ``created_at``, and reads bump ``last_used_at``. See
    decision/mem-6-design-write-supersedes-read-collapses.)
    """
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    all_writers = user_id == "*" and scope == "project"
    pool = await get_pool()
    async with pool.acquire() as conn:
        if all_writers:
            row = await conn.fetchrow(
                """
                SELECT namespace, key, value, scope, user_id, project, tags,
                       tags_search, created_at
                FROM memories
                WHERE namespace = $1 AND key = $2 AND scope = $3
                  AND project IS NOT DISTINCT FROM $4
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND COALESCE(metadata->>'status', '') NOT IN ('superseded', 'deletion_requested')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                namespace,
                key,
                scope,
                project,
            )
            if row:
                # The touch targets the row actually returned — its real
                # writer, not the wildcard.
                user_id = row["user_id"]
        else:
            row = await conn.fetchrow(
                """
                SELECT namespace, key, value, scope, user_id, project, tags, tags_search, created_at
                FROM memories
                WHERE namespace = $1 AND key = $2 AND scope = $3
                  AND user_id IS NOT DISTINCT FROM $4
                  AND project IS NOT DISTINCT FROM $5
                  AND (expires_at IS NULL OR expires_at > NOW())
                  -- MEM-8: superseded rows stay reachable by exact key
                  -- (history), but a deletion-flagged row is content whose
                  -- exposure must END at flag time — no default read path
                  -- may serve it while it awaits the purge review.
                  AND COALESCE(metadata->>'status', '') <> 'deletion_requested'
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
    include_superseded: bool = False,
) -> list[MemoryItem]:
    """Hybrid vector + trigram search, scoped to namespace(s) and user.

    Superseded rows are EXCLUDED by default (MEM-3): a corrected note that
    still ranks in search is still giving instructions — measured 2026-08-10
    beating its own correction at every startup-sweep limit. History is not
    gone: ``include_superseded=True`` returns them, marked, for audit reads.

    ``user_id="*"`` spans every writer in the partition. It is honored ONLY for
    ``scope=project``, where the ``project`` column already does the scoping and
    the ``user_id`` predicate is pure exclusion: a note written by one principal
    is otherwise invisible to a peer searching the same project, which made
    shared project memory unreachable for every agent but its author (MEM-5).

    It grants nothing new — permission is enforced at the NAMESPACE level, and
    any writer's rows were already readable by naming that writer explicitly.
    For every other scope the wildcard is ignored and treated as a literal,
    because there ``user_id`` identifies a PERSON (scope=user) or a HOST
    (scope=machine) and spanning it would be a disclosure, not a fix.
    """
    _, _, scope, user_id, project = _normalize_key_fields(
        scope=scope, user_id=user_id, project=project
    )
    all_writers = user_id == "*" and scope == "project"
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
                    metadata->>'status' AS lifecycle_status,
                    1 - (embedding <=> $1) AS vec_score,
                    similarity(search_text, $2) AS trgm_score
                FROM memories
                WHERE (expires_at IS NULL OR expires_at > NOW())
                  AND namespace = ANY($3::text[])
                  AND scope = $4
                  AND scope <> 'inbox'
                  AND ($11 OR user_id IS NOT DISTINCT FROM $5)
                  AND project IS NOT DISTINCT FROM $6
                  AND ($12 OR COALESCE(metadata->>'status', '') NOT IN ('superseded', 'deletion_requested'))
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
            all_writers,
            include_superseded,
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
                    status=row["lifecycle_status"],
                )
            )
            keys_to_update.setdefault(row["namespace"], []).append(row["key"])

        for ns, keys in keys_to_update.items():
            await conn.execute(
                """UPDATE memories SET last_used_at = NOW()
                   WHERE namespace = $1 AND key = ANY($2) AND scope = $3
                     AND ($6 OR user_id IS NOT DISTINCT FROM $4)
                     AND project IS NOT DISTINCT FROM $5""",
                ns,
                keys,
                scope,
                user_id,
                project,
                all_writers,
            )

    return results


async def memory_keys(
    namespaces: list[str],
    prefix: str = "",
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
    limit: int = 500,
) -> tuple[list[dict], int]:
    """Deterministic key enumeration under a prefix (MEM-2).

    The verb between ``memory_get`` (exact match) and ``memory_search``
    (semantic): "list every key under ``wip/``", in key order, ALL matches,
    no embedding involved. Semantic search cannot establish ABSENCE — eight
    differently-phrased searches returning nothing is evidence, not proof —
    and the live case this exists for is "an agent was shut down mid-job:
    did it store anything?", which previously took direct SQL.

    Returns ``(entries, total)``. ``total`` is the full match count so a
    truncated listing can SAY it is truncated — a capped enumeration that
    looks complete would be the exact failure this verb exists to end.

    Deliberate choices:
    - Values are NOT returned (only their length). This is an index;
      ``memory_get`` is for reading. Keeps an unbounded-prefix listing from
      hauling the whole partition into a context window.
    - Superseded rows ARE listed, marked via ``status`` — a census that hides
      corrected rows could not prove a write happened, which is the question.
    - ``user_id="*"`` spans writers under exactly the search rule (MEM-5):
      honored only for ``scope=project``, ignored elsewhere.
    - Empty prefix lists the whole partition — that IS the absence check.
    """
    _, _, scope, user_id, project = _normalize_key_fields(
        scope=scope, user_id=user_id, project=project
    )
    all_writers = user_id == "*" and scope == "project"
    namespaces = [ns.lower() for ns in namespaces]
    # LIKE-escape the prefix so 'wip_' means those four characters, not
    # "wip followed by anything" — a prefix is a literal, never a pattern.
    escaped = (
        prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            r"""
            SELECT namespace, key, scope, user_id, project, tags, created_at,
                   last_used_at, length(value) AS value_chars,
                   metadata->>'status' AS lifecycle_status,
                   count(*) OVER () AS total
            FROM memories
            WHERE (expires_at IS NULL OR expires_at > NOW())
              AND namespace = ANY($1::text[])
              AND scope = $2
              AND scope <> 'inbox'
              AND ($5 OR user_id IS NOT DISTINCT FROM $3)
              AND project IS NOT DISTINCT FROM $4
              AND key LIKE $6 ESCAPE '\'
            ORDER BY key, namespace, user_id
            LIMIT $7
            """,
            namespaces,
            scope,
            user_id,
            project,
            all_writers,
            f"{escaped}%",
            limit,
        )
    total = rows[0]["total"] if rows else 0
    entries = [
        {
            "namespace": r["namespace"],
            "key": r["key"],
            "scope": r["scope"],
            "user_id": r["user_id"],
            "project": r["project"],
            "tags": r["tags"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "value_chars": r["value_chars"],
            "status": r["lifecycle_status"],
        }
        for r in rows
    ]
    return entries, total


async def memory_forget(
    namespace: str,
    key: str,
    scope: str = "user",
    user_id: str = "default",
    project: str | None = None,
    actor_principal: str | None = None,
    actor_is_admin: bool = False,
) -> bool:
    """Hard-delete a memory. Returns True if found and deleted.

    MEM-8 destruction gate: only the row's controller (custodian, falling back
    to owner) or an admin may delete. The check and the delete are one
    transaction with the row locked — a gate that releases the row before the
    delete has the race it exists to close (same discipline as OWN-1 and
    if_match).

    Two deliberate pass-throughs, both documented rather than silent:
    - ``owner IS NULL`` rows predate attribution; locking them away would make
      the legacy corpus undeletable by everyone to protect authorship nobody
      recorded (mirrors OWN-1's NULL rule).
    - ``actor_principal is None`` is legacy/anonymous auth mode — there is no
      identity to gate on, so behavior is unchanged there. The gate is only as
      strong as require_auth, which is the posture that makes every other gate
      real too.
    """
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if actor_principal is not None and not actor_is_admin:
                row = await conn.fetchrow(
                    """SELECT owner, custodian FROM memories
                       WHERE namespace = $1 AND key = $2 AND scope = $3
                         AND user_id IS NOT DISTINCT FROM $4
                         AND project IS NOT DISTINCT FROM $5
                       FOR UPDATE""",
                    namespace, key, scope, user_id, project,
                )
                if row is not None:
                    controller = row["custodian"] or row["owner"]
                    if controller is not None and controller != actor_principal:
                        raise ForgetDenied(controller, actor_principal)
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


async def memory_flag_deletion(
    namespace: str,
    key: str,
    scope: str,
    user_id: str,
    project: str | None,
    actor_principal: str | None,
    reason: str,
) -> dict | None:
    """MEM-8 verb 2: request physical destruction of a row you may not delete.

    Any namespace writer may flag; the flag hides the row from default reads
    IMMEDIATELY (supersede semantics — for a leaked credential the exposure
    window closes at flag time) and enqueues it for an admin/librarian to
    review and execute or reject. The row's prior lifecycle status is
    preserved in the stamp so a rejection can restore it exactly.

    Reaches the four data scopes (project/shared/user/machine); protocol
    scopes are rejected at the router like every other data verb. Rows
    already flagged are not re-stamped (first reason wins; the queue is the
    place to argue).
    """
    scope = (scope or "").lower()
    if scope not in ("project", "shared", "user", "machine"):
        raise ValueError(
            "flag_deletion reaches data scopes only "
            "(project/shared/user/machine)"
        )
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    if not (reason or "").strip():
        raise ValueError(
            "flag_deletion requires a reason — the reviewer who executes "
            "the destruction acts on it"
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            prior = await conn.fetchval(
                """SELECT metadata->>'status' FROM memories
                   WHERE namespace = $1 AND key = $2 AND scope = $3
                     AND user_id IS NOT DISTINCT FROM $4
                     AND project IS NOT DISTINCT FROM $5
                   FOR UPDATE""",
                namespace, key, scope, user_id, project,
            )
            stamp = {
                "status": "deletion_requested",
                "deletion_flagged_at": datetime.now(timezone.utc).isoformat(),
                "deletion_flagged_by_principal": actor_principal,
                "deletion_reason": reason.strip(),
            }
            if prior:
                stamp["deletion_prior_status"] = prior
            row = await conn.fetchrow(
                """
                UPDATE memories
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $6::jsonb
                WHERE namespace = $1 AND key = $2 AND scope = $3
                  AND user_id IS NOT DISTINCT FROM $4
                  AND project IS NOT DISTINCT FROM $5
                  AND COALESCE(metadata->>'status', '') <> 'deletion_requested'
                RETURNING namespace, key, scope, user_id, project, owner
                """,
                namespace, key, scope, user_id, project, json.dumps(stamp),
            )
    return dict(row) if row else None


async def deletion_queue_list(limit: int = 200) -> list[dict]:
    """Rows awaiting the librarian's destruction review, oldest flag first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT namespace, key, scope, user_id, project, owner, custodian,
                   metadata->>'deletion_flagged_at' AS flagged_at,
                   metadata->>'deletion_flagged_by_principal' AS flagged_by,
                   metadata->>'deletion_reason' AS reason
            FROM memories
            WHERE metadata->>'status' = 'deletion_requested'
            ORDER BY metadata->>'deletion_flagged_at' NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def deletion_queue_reject(
    namespace: str,
    key: str,
    scope: str,
    user_id: str,
    project: str | None,
    actor_principal: str | None,
    reason: str,
) -> dict | None:
    """Admin declines a deletion request: restore the row's prior status.

    The flag stamps stay in metadata (renamed to ``deletion_rejected_*``) so
    the request and its outcome remain readable — the queue is an audit
    surface, not a scratchpad.
    """
    namespace, key, scope, user_id, project = _normalize_key_fields(
        namespace, key, scope, user_id, project
    )
    if not (reason or "").strip():
        raise ValueError("rejecting a deletion request requires a reason")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            md_raw = await conn.fetchval(
                """SELECT metadata FROM memories
                   WHERE namespace = $1 AND key = $2 AND scope = $3
                     AND user_id IS NOT DISTINCT FROM $4
                     AND project IS NOT DISTINCT FROM $5
                     AND metadata->>'status' = 'deletion_requested'
                   FOR UPDATE""",
                namespace, key, scope, user_id, project,
            )
            if md_raw is None:
                return None
            md = json.loads(md_raw) if isinstance(md_raw, str) else dict(md_raw)
            prior = md.pop("deletion_prior_status", None)
            if prior:
                md["status"] = prior
            else:
                md.pop("status", None)
            md["deletion_rejected_at"] = datetime.now(timezone.utc).isoformat()
            md["deletion_rejected_by_principal"] = actor_principal
            md["deletion_rejected_reason"] = reason.strip()
            row = await conn.fetchrow(
                """
                UPDATE memories SET metadata = $6::jsonb
                WHERE namespace = $1 AND key = $2 AND scope = $3
                  AND user_id IS NOT DISTINCT FROM $4
                  AND project IS NOT DISTINCT FROM $5
                RETURNING namespace, key, scope, user_id, project
                """,
                namespace, key, scope, user_id, project, json.dumps(md),
            )
    return dict(row) if row else None


async def estate_transfer(
    from_principal: str,
    to_principal: str,
    namespace: str | None = None,
    project: str | None = None,
    dry_run: bool = False,
) -> int:
    """MEM-8 verb 4: reassign a departed principal's destruction rights.

    Sets ``custodian`` on every data-scope row the departed principal
    controls (custodian = from, or custodian unset and owner = from).
    ``owner`` is NEVER touched — attribution is immutable by construction;
    a transferred row reads ``owner=<author>, custodian=<heir>``.

    Admin-only (enforced at the router). Deliberately excludes protocol
    scopes: inbox/presence/seat rows are addressing state where user_id
    means destination, not authorship — an estate has no claim there.
    Correction needs no transfer (supersede is already open to successors);
    this moves only the right to destroy, which is why it is a deliberate
    owner-level act and not something a peer can infer.
    """
    from_p = (from_principal or "").strip().lower()
    to_p = (to_principal or "").strip().lower()
    if not from_p or not to_p:
        raise ValueError("estate_transfer requires from_principal and to_principal")
    if from_p == to_p:
        raise ValueError("estate_transfer from and to are the same principal")
    where = """
        (custodian = $1 OR (custodian IS NULL AND owner = $1))
        AND scope IN ('project', 'shared', 'user', 'machine')
        AND ($3::text IS NULL OR namespace = $3)
        AND ($4::text IS NULL OR project = $4)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if dry_run:
            # $2 (the heir) appears in a tautology so the prepared statement
            # can type it — the count must bind identically to the update.
            return await conn.fetchval(
                f"SELECT COUNT(*) FROM memories WHERE {where} AND $2::text IS NOT NULL",
                from_p, to_p, namespace, project,
            )
        result = await conn.execute(
            f"UPDATE memories SET custodian = $2 WHERE {where}",
            from_p, to_p, namespace, project,
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def partition_siblings(
    namespaces: list[str],
    key: str,
    project: str | None,
    exclude_user_id: str | None = None,
) -> list[str]:
    """Writers OTHER than the caller holding this same key in one project.

    Project memory is partitioned per writer, so the same key can exist once
    per principal — and every verb that resolves by key sees only one
    partition. This helper is what lets those verbs be HONEST about it: a get
    or forget that misses can say "exists under writer X" instead of "not
    found", and a store can say "you just forked a duplicate" instead of
    "stored". Measured 2026-08-10 (softphone): all three verbs reported a
    cross-writer collision as a clean slate, and the cleanup stalled on it.
    """
    _, key, scope, exclude, project = _normalize_key_fields(
        key=key, scope="project", user_id=exclude_user_id or "", project=project
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id FROM memories
            WHERE namespace = ANY($1::text[]) AND key = $2 AND scope = 'project'
              AND project IS NOT DISTINCT FROM $3
              AND user_id IS DISTINCT FROM $4
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY user_id
            """,
            [ns.lower() for ns in namespaces],
            key,
            project,
            exclude or None,
        )
    return [r["user_id"] for r in rows]


async def memory_supersede(
    namespaces: list[str],
    key: str,
    project: str | None,
    target_user_id: str,
    actor_principal: str | None,
    reason: str,
    replacement_key: str | None = None,
    scope: str = "project",
) -> dict | None:
    """Mark ANOTHER writer's project row as superseded — correction, not deletion.

    MEM-3, built the day it bit (2026-08-10): a departed agent's project notes
    went stale, the successor could neither edit them (owner-enforced) nor
    delete them (partition-scoped), and its correction rows ranked BELOW the
    stale text in the searches every startup sweep runs. Agents trust project
    notes precisely because agents wrote them, so a stale row left retrievable
    is an instruction to the next reader.

    The design line: **corrections change what readers retrieve, not what
    history recorded.** The row is kept verbatim — value untouched, writer
    untouched, retrievable via include_superseded — and gains lifecycle
    metadata naming who superseded it, when, why, and what replaces it. That
    is the before/after audit trail. Default search stops returning it, which
    is the channel that actually misleads (corrections-beside-stale measured
    losing at startup limits).

    Permission: the caller needs READ on the row's namespace, nothing more.
    This is deliberate and narrower than it looks — scope is limited to
    'project' and 'shared' (user/machine scopes are personal and keep their
    privacy), the content is untouched, the action is fully attributed to the
    authenticated principal, and OWN-1's ownership gate still protects the
    row's VALUE. A project's members were always allowed to disagree with a
    note; this makes the disagreement machine-readable instead of a losing
    race in vector space. Copies the inbox lifecycle pattern (same table,
    metadata status, no schema migration; NULL status reads as live, so it
    is back-compatible).

    scope='shared' added for MEM-7 (2026-08-15): the shared lesson corpus is
    the retirement verb's primary curation target (882 rows, 0 ever
    superseded — because the verb could not reach them). Same contract as
    project: kept verbatim, attributed, reversible, drained from default
    search only. Shared rows carry no project, so the project filter is
    forced NULL there rather than trusting the caller's resolved project.
    """
    scope = (scope or "project").lower()
    if scope not in ("project", "shared"):
        raise ValueError(
            "supersede reaches scope 'project' or 'shared' only — "
            "user/machine scopes are personal"
        )
    if scope == "shared":
        project = None
    _, key, _, target, project = _normalize_key_fields(
        key=key, scope=scope, user_id=target_user_id, project=project
    )
    if not (reason or "").strip():
        raise ValueError("supersede requires a reason — it becomes the audit trail")
    stamp = {
        "status": "superseded",
        "superseded_at": datetime.now(timezone.utc).isoformat(),
        "superseded_by_principal": actor_principal,
        "superseded_reason": reason.strip(),
    }
    if replacement_key:
        stamp["superseded_by_key"] = replacement_key.strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE memories
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $6::jsonb
            WHERE namespace = ANY($1::text[]) AND key = $2 AND scope = $7
              AND user_id IS NOT DISTINCT FROM $3
              AND project IS NOT DISTINCT FROM $4
              AND COALESCE(metadata->>'status', '') <> $5
            RETURNING namespace, key, user_id, project
            """,
            [ns.lower() for ns in namespaces],
            key,
            target,
            project,
            "superseded",
            json.dumps(stamp),
            scope,
        )
    return dict(row) if row else None


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
        # MSG-10 stored `machine` but never surfaced it, so a five-box fleet
        # still could not see which box a message came from — the same
        # dropped-at-the-last-step shape that item was written to fix, one layer
        # later. Mapped here with the model fields rather than left for a third
        # pass over the same two functions.
        machine=md.get("machine"),
        model=md.get("model"),
        model_source=md.get("model_source"),
        from_lane=md.get("from_lane"),
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
    machine: str | None = None,
    model: str | None = None,
    model_source: str | None = None,
    from_lane: str | None = None,
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
        # MSG-10: which BOX this was sent from. Every client has always
        # stamped X-Engram-Machine from its hostname and only `/memory/set`
        # ever read it, so mail could not be attributed to a machine after the
        # fact — the field was on the wire and dropped at the last step, the
        # same shape as the render that discarded `created_at`.
        #
        # Client-supplied, so it is provenance and NOT proof: unlike
        # `from_principal` it is not derived from the token and a caller could
        # set it to anything. Useful for "where did this come from", never for
        # a trust decision.
        "machine": machine,
        # MODEL-RECORD-1: which MODEL produced this message. Memory rows have
        # carried it since ea7fc76 while mail did not, so the surface where a
        # claim gets ACTED on was the one that could not say what produced it.
        #
        # Like `machine` and unlike `from_principal`, this is client-supplied
        # provenance, not proof — the bridge reads it from the harness's own
        # record rather than asking the agent, but nothing server-side verifies
        # it. Good for "what wrote this", never for a trust decision.
        #
        # `model_source` is stored even when the model is unknown, so a reader
        # can tell "that harness records nothing" from "this predates the stamp".
        "model": model,
        "model_source": model_source,
        # LANE-5: sender's immortal lane, bridge-stamped like listen_set —
        # what a reply targets so it outlives the sender's seat.
        "from_lane": from_lane,
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

# SEAT-4 retention horizon: a presence row silent this long is dropped from
# the default roster. It is NOT a liveness test and must not be confused with
# one — MSG-8 measured that a busy agent head-down in a long tool call is
# silent in exactly the way a dead one is, which is why no SHORT staleness
# window may ever mark a session dead.
#
# That argument bounds how short a horizon can be. It does not forbid one.
# No tool call runs for two days, so at this scale "silent" and "gone" stop
# being distinguishable in any way that matters, and the cost of the two
# errors is wildly asymmetric: hiding a two-day-silent session that somehow
# lives costs one `include_stale=True`; showing sixteen corpses cost the owner
# the ability to use his own huddle picker at all, because the live sessions
# he needed were buried under dead ones still advertising "running".
#
# Hidden, never deleted — the rows stay queryable, same doctrine as inbox mail.
PRESENCE_RETENTION_SECONDS = 172800  # 48h silent → off the default roster


# Seat-collision detection (grew out of ROST-1): a session nonce is fresh for
# this many seconds. Window ≈ 2.5× the bridge heartbeat interval (120s) — a
# bridge restart mid-session can look like a collision for at most this long.
#
# ⚠️ WHAT THIS CANNOT SEE, stated because the omission is not obvious and a
# peer reasoned from the assumption that it was covered (2026-08-01). Detection
# needs BOTH sessions in the nonce map, and a nonce only lands there via
# `presence_update` — i.e. via the bridge heartbeat, which rides TOOL CALLS.
# The bridge has no background beat, and `_claim_seat` is reachable only from
# that same heartbeat and from `memory_take_seat`.
#
# So SEAT ALLOCATION IS LAZY: a session claims on its FIRST ENGRAM TOOL CALL,
# not at startup. A second session in the same folder that is idle, or busy
# with work that never touches engram, never heartbeats — so it never enters
# the map, and a collision involving it is STRUCTURALLY UNDETECTABLE. Measured
# by AgentBeast: this is not a settling window a session passes through, it is
# a state a session can occupy for its entire life.
#
# The detector is therefore sound for what it reports and silent — not
# reassuring — about the lazy case. Do not read "no collision" as "one session
# here". See SEAT-15.
SEAT_COLLISION_WINDOW_SECONDS = 300
# Identities where multiple simultaneous sessions are legitimate role-sharing
# by design (per the two-axis doctrine): never flagged as collisions.
SEAT_EXEMPT_IDENTITIES = {"admin"}

# MSG-5/SEAT-7: a watcher beat is fresh for this long. Window ≈ 6.6× the
# watcher's 45s poll interval, so a slow poll or one dropped request never
# reads as a dead ear.
WATCHER_STALE_AFTER_SECONDS = 300


def _fresh_sessions(
    md: dict, now: datetime, superseded: set[str] | None = None
) -> dict:
    """Return the still-fresh entries of a presence row's nonce map.

    SEAT-12: a nonce that has been DISPLACED from its seat is not a rival, it
    is a corpse. Without that distinction the detector fired on every restart
    — the dead predecessor stayed inside the freshness window for five minutes
    and got counted as a second live session, so a session that had merely
    been restarted was reported as a seat collision. The code carried a
    comment conceding it, because until SEAT-9 recorded `superseded_nonces`
    nothing could tell a corpse from a rival.
    """
    superseded = superseded or set()
    fresh = {}
    for nonce, info in (md.get("sessions") or {}).items():
        if nonce in superseded:
            continue
        try:
            seen = datetime.fromisoformat(info.get("last_seen"))
        except (TypeError, ValueError):
            continue
        if (now - seen).total_seconds() < SEAT_COLLISION_WINDOW_SECONDS:
            fresh[nonce] = info
    return fresh


def _superseded_from_seat(md: dict | str | None) -> set[str]:
    """The displaced-nonce set recorded on a seat row by SEAT-9's one-way door."""
    if not md:
        return set()
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except (TypeError, ValueError):
            return set()
    raw = md.get("superseded_nonces") or []
    return {n for n in raw if isinstance(n, str)}


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
        # SEAT-12: nonces this identity's seat has already displaced are
        # corpses, not rivals. Read them here so the collision check below
        # cannot count a restarted session's dead predecessor as a second live
        # session — the false positive that fired on EVERY restart.
        seat_row = await conn.fetchrow(
            """
            SELECT metadata FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND project = $4 AND key = $5
            """,
            PRESENCE_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
            project, f"seat/{identity}",
        )
        superseded = _superseded_from_seat(seat_row["metadata"] if seat_row else None)
        # Merge this beat into the pruned nonce map. Legacy clients (no nonce)
        # don't participate in collision tracking but keep normal presence.
        sessions = _fresh_sessions(prior_md, now, superseded)
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
        # GENERATIONAL GUARD (2026-07-27, after a power outage falsified the
        # first version within four hours): carry it forward only WITHIN A
        # GENERATION. `watcher_last_seen` describes the process that armed that
        # watcher. It survives on a row the NEXT generation reclaims through
        # SEAT-9 continuity, so after a restart the dead generation's evidence
        # was being attributed to the live one — a fact about process N served
        # as a fact about process N+1.
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
        # it, so a genuine corpse (no beats at all) keeps its last watcher beat
        # on the row, correctly attributed, for consumers to judge.
        #
        # Legacy clients send no nonce and cannot be generation-checked; they
        # keep the unconditional carry-forward, i.e. today's behaviour.
        prior_sessions = prior_md.get("sessions") or {}
        new_generation = bool(session_nonce) and session_nonce not in prior_sessions
        watcher_seen = prior_md.get("watcher_last_seen")
        if watcher_seen and not new_generation:
            metadata["watcher_last_seen"] = watcher_seen
        # REVOCATION, and note it is by OMISSION: `metadata` is built fresh, so
        # a prior `farewell_at` is dropped unless deliberately carried forward,
        # and a heartbeat is the strongest possible evidence of life. Stated
        # explicitly because it currently works by construction, and the next
        # person to "fix" this by carrying the whole prior dict forward would
        # silently resurrect every farewell the session had already disproved.
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
        # REVOCATION (2026-08-01): a beat here is evidence of life, and any
        # evidence of life VOIDS a farewell — hence the `- 'farewell_at'`.
        #
        # Without it a wrong farewell is silent AND permanent: the session it
        # libelled cannot correct the record, because being unheard is the
        # premise of the mistake.
        #
        # ⚠️ HOW FAR THIS ACTUALLY GETS — corrected 2026-08-01 after AgentBeast
        # read the code rather than this comment, which had claimed revocation
        # heals "the moment a re-armed watcher or the session itself speaks".
        # VERIFIED, and BOTH of those speakers are absent in the case that
        # matters most:
        #   · The watcher cannot re-arm. It sends the farewell and exits on the
        #     same branch (inbox_wait.py), and arming happens at session start —
        #     which, by hypothesis, did not happen.
        #   · An IDLE session never speaks. The bridge has no background beat
        #     at all (no create_task, no timer); every heartbeat rides a tool
        #     call. A session that is alive and idle emits nothing.
        # So revocation reliably heals only a session that goes on to DO WORK —
        # which is the population LEAST likely to be falsely declared dead,
        # because a busy session's process is plainly there.
        #
        # Partial mitigation, stated as a fact and not a defence: when the
        # watcher is harness-managed, its exit is itself reported to the
        # session, which typically provokes the tool call that heals the row.
        # That covers harness-armed sessions and NOT a bare-shell watcher, so
        # it narrows the gap without closing it.
        #
        # This comment previously described the property we wanted. Prose one
        # layer above what the code does is the exact failure our own huddle
        # doc committed, and an unmitigated residual DOCUMENTED AS MITIGATED is
        # worse than a small residual honestly labelled. See SEAT-13.
        updated = await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb) - 'farewell_at',
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


async def presence_farewell(identity: str, project: str) -> bool:
    """Record that a watcher OBSERVED this session's process exit.

    Narrow, like presence_watcher_beat, and for the same reasons: merges with
    jsonb_set so nothing the session reported is clobbered, and REFUSES TO
    INSERT — no presence row means no session ever heartbeated here, and
    conjuring one from a farewell would invent a session in order to declare
    it dead.

    Deliberately does NOT touch last_used_at. A farewell is news about the
    session, not evidence that anything is alive at this address; refreshing
    the clock would extend the very lease the farewell exists to shorten.

    ⚠️ The asymmetry is the whole design. A farewell RECEIVED is evidence of
    death. A farewell NOT received is evidence of NOTHING — a watcher that was
    killed itself sends nothing, and so does a machine that lost power. Any
    code that reads absence as a signal has rebuilt the 600-second window under
    a better name.
    """
    identity = identity.lower()
    project = project.lower()
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        updated = await conn.execute(
            """
            UPDATE memories
            SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{farewell_at}', to_jsonb($1::text), true
                )
            WHERE namespace = $2 AND scope = $3 AND user_id = $4 AND key = $5
            """,
            now.isoformat(),
            PRESENCE_NAMESPACE, PRESENCE_SCOPE, project, f"presence/{identity}",
        )
    return updated != "UPDATE 0"


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
    include_expired: bool = False,
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
        # SEAT-12: same displaced-nonce exclusion the heartbeat applies, so the
        # roster and the presence response cannot disagree about whether an
        # identity is colliding. One query for every seat in scope rather than
        # one per entry.
        seat_rows = await conn.fetch(
            """
            SELECT key, metadata FROM memories
            WHERE namespace = $1 AND scope = $2 AND user_id = $3
              AND ($4::text IS NULL OR project = $4)
            """,
            PRESENCE_NAMESPACE, SEAT_SCOPE, SEAT_USER_ID,
            project.lower() if project else None,
        )
    superseded_by_ident = {
        r["key"].removeprefix("seat/"): _superseded_from_seat(r["metadata"])
        for r in seat_rows
    }
    now = datetime.now(timezone.utc)
    entries = []
    for r in rows:
        md = r["metadata"] or {}
        if isinstance(md, str):
            md = json.loads(md)
        # No default. A row whose metadata says nothing about state is a row
        # that told us nothing — the old `or "running"` manufactured a claim
        # for exactly the sessions that had never made one.
        state = md.get("state")
        if state == "done" and not include_done:
            continue
        if channel and channel.lower() not in [c.lower() for c in md.get("channels") or []]:
            continue
        last_seen = r["last_used_at"] or now
        age = (now - last_seen).total_seconds()
        # SEAT-4 retention horizon. Rows are HIDDEN, never deleted — pass
        # include_expired to get them back. A roster whose job is "who can I
        # talk to right now" is actively harmful when two days of corpses bury
        # the handful of live sessions: the owner could not reach the start
        # button in his own huddle picker, which is a roster consumer.
        if age >= PRESENCE_RETENTION_SECONDS and not include_expired:
            continue
        ident = r["key"].removeprefix("presence/")
        fresh = _fresh_sessions(md, now, superseded_by_ident.get(ident))
        live = max(len(fresh), 1)
        watcher_alive, watcher_seen = _watcher_state(md, now)
        # FACTS, NEVER VERDICTS (decision/roster-should-report-facts-not-
        # judgments, 2026-07-27; `state` dropped from the payload 2026-08-01).
        # Every signal available here is a heuristic — a busy agent and a dead
        # one are both silent (MSG-8), and a machine cannot write its own
        # goodbye — so any verdict computed from this row has a failure mode;
        # the shipped `presumed-dead` override was falsified by a power cut the
        # same day it landed. The roster serves what it can attest: the address
        # exists, when the session last spoke, when its watcher last beat.
        # Consumers judge — and the consumer that judges WELL is whatever
        # spawned the session, because it observes a termination rather than
        # inferring one from silence.
        entries.append({
            "identity": ident,
            "project": r["user_id"],
            # Back-compat shim — see RosterEntry.state. A pre-2026-08-01 bridge
            # subscripts this directly and KeyErrors without it, which broke
            # `memory_roster` for every session already running at deploy time.
            # "unknown" rather than the old invented "running" default.
            "state": state or "unknown",
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
            # A fact, served like every other: "a watcher observed this
            # session's process exit at T". Absent means no such observation
            # was ever made, which is NOT a claim that the session is alive.
            "farewell_at": md.get("farewell_at"),
        })
    return entries


async def recipient_liveness(addresses: list[str]) -> dict[str, dict]:
    """Liveness for each address that HAS a presence row. Absence is omitted.

    Used to warn a sender at the moment of the mistake: a message whose whole
    purpose is coordination (``intent=action|proceed|escalate``) sent to a
    session that stopped heartbeating two days ago cannot achieve that
    purpose, and today the send reports plain success. A peer spent a turn
    dividing work with a counterparty that had been dead 42 hours; the roster
    would have said so in one call, and the call was never made. Putting the
    answer in the send response removes the need to remember to ask.

    Deliberately NOT a check on ``intent=fyi``. Sending to a not-yet-running
    session is legitimate and frequent — queued mail is a feature, and the
    owner has said so explicitly. The distinction is PURPOSE, which the
    ``intent`` field already carries.

    ABSENT IS NOT DEAD, enforced here by omission: an address with no presence
    row simply does not appear in the result, so callers cannot render "no
    row" as "dead". That conflation is the root of most of this defect class,
    and a brand-new session that has never heartbeated is exactly the case
    that must not be flagged.
    """
    addresses = [a.lower() for a in addresses if a]
    if not addresses:
        return {}
    keys = [f"presence/{a}" for a in addresses]
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT key, user_id, metadata, last_used_at
            FROM memories
            WHERE namespace = $1 AND scope = $2 AND key = ANY($3::text[])
            """,
            PRESENCE_NAMESPACE, PRESENCE_SCOPE, keys,
        )
    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for r in rows:
        md = r["metadata"] or {}
        if isinstance(md, str):
            md = json.loads(md)
        ident = r["key"].removeprefix("presence/")
        last_seen = r["last_used_at"] or now
        age = (now - last_seen).total_seconds()
        watcher_alive, _ = _watcher_state(md, now)
        # Facts only, same discipline as the roster. The caller composing a
        # warning gets watcher_alive and age and decides what they mean — the
        # store attests, it does not judge. (`state` was dropped here too on
        # 2026-08-01: no caller read it, and it only ever said "running".)
        out[ident] = {
            "age_seconds": round(age, 1),
            "is_stale": age >= PRESENCE_STALE_AFTER_SECONDS,
            "watcher_alive": watcher_alive,
            # An OBSERVED exit, when one exists. This is the only signal here
            # that does not have to wait out a window: staleness needs 10
            # minutes and a silent watcher 5, and a seat abandoned inside those
            # warns about nothing. A watcher that saw the process go reports it
            # on its next poll.
            "farewell_at": md.get("farewell_at"),
        }
    return out


async def inbox_unread_count(
    listen_set: list[str],
    reader_identity: str | None,
) -> int:
    """How many open messages this reader has not acked. A COUNT, not a page.

    Deliberately mirrors ``inbox_list``'s unread predicate exactly — same
    archived/status/read_by clauses — so the number and the listing can never
    describe different sets. It does NOT reuse
    ``inbox_unread_by_sender``: that one excludes group traffic on purpose
    (a fan-out message is not a personal obligation, so it must not sit on a
    per-sender badge), whereas the banner answers "is there mail here at all",
    for which huddle and fan-out mail plainly counts.

    Exists because the banner used to report ``len(msgs)`` from a list fetched
    with ``LIMIT preview_limit + 1``, so its "unread" was structurally
    incapable of exceeding 6 — a page size wearing a count's clothes. A
    session sitting on 130 open messages was told it had 6, and 6 is small
    enough to look like a real answer rather than an obvious truncation.
    Reported by a peer who saw three different numbers for one mailbox
    (banner 6, listing 20, digest 130) and concluded it could trust none of
    them.
    """
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT count(*)
            FROM memories
            WHERE namespace = $1
              AND scope = $2
              AND user_id = ANY($3::text[])
              AND COALESCE((metadata->>'archived')::bool, false) = false
              AND COALESCE(metadata->>'status', $4) = $4
              AND (
                  $5::text IS NULL
                  OR NOT COALESCE(metadata->'read_by', '[]'::jsonb) ? $5::text
              )
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, listen_set, INBOX_OPEN,
            reader_identity.lower() if reader_identity else None,
        )


async def inbox_banner(
    listen_set: list[str],
    reader_identity: str | None,
    preview_limit: int = 5,
) -> dict | None:
    """Return ``{unread_count, shown, preview}`` if there is unread mail.

    ``unread_count`` is the TRUE total; ``shown`` is how many of them the
    preview lists. Keeping both means a caller can say "6 of 130" instead of
    silently presenting the window size as the total.
    """
    listen_set = [addr.lower() for addr in listen_set]
    if not listen_set:
        return None
    total = await inbox_unread_count(listen_set, reader_identity)
    if not total:
        return None
    msgs = await inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=True,
        limit=preview_limit,
        newest_first=True,
    )
    preview = []
    for m in msgs[:preview_limit]:
        sender = m.from_ or "unknown"
        subject = m.subject or (m.body[:60] + ("…" if len(m.body) > 60 else ""))
        stale = f" ⚠️ STALE ({int(m.age_hours // 24)}d — verify)" if m.is_stale else ""
        preview.append(f"{sender} → {m.to}: {subject}{stale}")
    return {"unread_count": total, "shown": len(preview), "preview": preview}


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


async def inbox_resolve_thread(
    thread_id: str,
    listen_set: list[str],
    resolver_identity: str | None = None,
) -> int:
    """Resolve every open message in a thread that was delivered to this reader.

    The gap this closes: a room gets closed and its mail stays ``open``
    forever. A peer reported reading twenty present-tense messages from a
    huddle the owner had shut days earlier — "standing by", "I will not race
    you" — and concluded a counterparty was waiting on it. Nothing in a
    durable message says the conversation is over, so the whole thread keeps
    reading as live. Per-message resolve exists, but a 20-message room needs
    20 calls and a 130-message backlog is simply not drainable by hand, so in
    practice nobody drains anything.

    SCOPED TO THE CALLER'S OWN MAIL, deliberately: ``user_id = ANY(listen_set)``
    means this can only resolve copies addressed to you. Resolving a thread is
    a statement about YOUR handling of it, not a claim over everyone else's
    inbox — a fan-out lands one row per recipient, and one participant
    declaring the room finished must not drain it out from under the others.

    Returns the number of messages resolved (0 if the thread is unknown or
    already drained — idempotent, so a closer can call it without checking).
    """
    listen_set = [a.lower() for a in listen_set if a]
    if not thread_id or not listen_set:
        return 0
    resolved_at = datetime.now(timezone.utc).isoformat()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
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
            WHERE namespace = $4 AND scope = $5
              AND user_id = ANY($6::text[])
              AND metadata->>'thread_id' = $7
              AND COALESCE(metadata->>'status', $8) = $8
            RETURNING key
            """,
            INBOX_RESOLVED,
            resolved_at,
            resolver_identity,
            INBOX_NAMESPACE,
            INBOX_SCOPE,
            listen_set,
            thread_id,
            INBOX_OPEN,
        )
    return len(rows)


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
