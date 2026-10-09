import logging
import re
import asyncpg
from pgvector.asyncpg import register_vector

from server.config import settings

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS memories (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'user',
    user_id         TEXT NOT NULL DEFAULT 'default',
    project         TEXT,
    tags            TEXT NOT NULL DEFAULT '',
    tags_search     TEXT NOT NULL DEFAULT '',
    embedding       vector(768),
    search_text     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    metadata        JSONB,
    owner           TEXT,
    custodian       TEXT,
    UNIQUE NULLS NOT DISTINCT (namespace, key, scope, user_id, project)
);

CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw ON memories
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories (key);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories (scope);
CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories (user_id);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories (namespace);
CREATE INDEX IF NOT EXISTS idx_memories_ns_scope_uid ON memories (namespace, scope, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_search_text_trgm ON memories
    USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_expires_at ON memories (expires_at)
    WHERE expires_at IS NOT NULL;

-- Principals: identity & access control
CREATE TABLE IF NOT EXISTS principals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL UNIQUE,
    type                TEXT NOT NULL CHECK (type IN ('human', 'agent')),
    is_admin            BOOLEAN NOT NULL DEFAULT FALSE,
    token_hash          TEXT,
    -- SHA-256 hex of the raw token: O(1) indexed lookup instead of a
    -- full bcrypt scan per auth attempt (auth-spray DoS). bcrypt hash
    -- stays the verifier; this only narrows the candidate row.
    token_lookup        TEXT,
    password_hash       TEXT,
    read_namespaces     TEXT[] NOT NULL DEFAULT '{}',
    write_namespaces    TEXT[] NOT NULL DEFAULT '{}',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_principals_type ON principals (type);
CREATE INDEX IF NOT EXISTS idx_principals_active ON principals (id)
    WHERE active = TRUE;
-- idx_principals_token_lookup lives in MIGRATE_SQL: on an existing DB this
-- CREATE TABLE no-ops, so an index here would reference the column before
-- the migration adds it.

CREATE TABLE IF NOT EXISTS principal_aliases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id        UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    alias               TEXT NOT NULL,
    source              TEXT NOT NULL,
    UNIQUE (alias, source)
);
CREATE INDEX IF NOT EXISTS idx_principal_aliases_principal ON principal_aliases (principal_id);
CREATE INDEX IF NOT EXISTS idx_principal_aliases_alias ON principal_aliases (alias);

CREATE TABLE IF NOT EXISTS consent_grants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    granter_id          UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    grantee_id          UUID NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_consent_grants_granter ON consent_grants (granter_id);
CREATE INDEX IF NOT EXISTS idx_consent_grants_grantee ON consent_grants (grantee_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id            UUID REFERENCES principals(id) ON DELETE SET NULL,
    action                  TEXT NOT NULL,
    target_principal_id     UUID REFERENCES principals(id) ON DELETE SET NULL,
    detail                  TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_principal ON audit_log (principal_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log (created_at);

-- OBS-REQLOG-1: the request trail. Before this, nothing could answer "which
-- credential called what, and when" for any surface, ever — which is why an
-- identity question once had to be settled by hashing tokens, and why
-- narrowing the public allowlist was a guess rather than a measurement.
-- DELIBERATELY NOT RECORDED: request bodies, and query-string VALUES (the
-- path is stored with its query stripped). The rows name principals, so
-- retention is bounded (ENGRAM_REQUEST_LOG_RETENTION_DAYS, pruned by the
-- existing cleanup loop) rather than kept forever.
CREATE TABLE IF NOT EXISTS request_log (
    id            BIGSERIAL PRIMARY KEY,
    principal     TEXT,
    auth_source   TEXT,
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    status        INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    -- True when the request carried the public-edge tag (PUBLIC-SURFACE-2
    -- header). Lets the trail separate internet traffic from tailnet/local —
    -- the question PUBLIC-SURFACE-1 has to answer before the allowlist is cut.
    via_public    BOOLEAN NOT NULL DEFAULT FALSE,
    -- OBS-SESSION-1: WHICH BOX and WHICH PROJECT, not just which credential.
    -- Every agent on the fleet authenticates as the same principal, so
    -- `principal` alone cannot separate this host from the rest of the fleet —
    -- measured 2026-08-26, when answering "what does one session cost" needed
    -- local `ps` plus arithmetic and still could not see the remote boxes.
    -- Both values ride provenance headers the bridge ALREADY sends, so this
    -- costs no client change and no fleet sweep.
    machine       TEXT,
    project       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_request_log_created ON request_log (created_at);
-- idx_request_log_public lives in MIGRATE_SQL: on an existing DB this block
-- runs BEFORE the via_public column is added, and an index on a column that
-- does not exist yet aborts the whole SCHEMA_SQL transaction. Same class as
-- idx_principals_token_lookup above — the upgrade-ordering bug, repeated
-- once (2026-08-25) and caught by the test DB before it reached prod.
CREATE INDEX IF NOT EXISTS idx_request_log_principal ON request_log (principal, created_at);
CREATE INDEX IF NOT EXISTS idx_request_log_path ON request_log (path, created_at);

-- WEBPUSH-1: the mail signal. One row per issued capability URL. Only the
-- SHA-256 of the key is stored — the raw key is shown once at issue and never
-- again, so a DB dump cannot be turned into working signal URLs. A row is
-- dead once revoked_at is set; re-issuing for an address revokes the old row
-- (rotation). last_polled_at/poll_count answer "is the agent's hook really
-- polling?" without anyone having to ask the agent.
CREATE TABLE IF NOT EXISTS mail_signal (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT NOT NULL,
    principal       TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,
    issued_by       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    last_polled_at  TIMESTAMPTZ,
    poll_count      BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mail_signal_address ON mail_signal (address);
"""

# Migration: add namespace column to tables created before this column existed.
MIGRATE_SQL = """
ALTER TABLE request_log ADD COLUMN IF NOT EXISTS via_public BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE request_log ADD COLUMN IF NOT EXISTS machine TEXT;
ALTER TABLE request_log ADD COLUMN IF NOT EXISTS project TEXT;
CREATE INDEX IF NOT EXISTS idx_request_log_machine ON request_log (machine, created_at);
CREATE INDEX IF NOT EXISTS idx_request_log_public ON request_log (via_public, created_at);
DO $$
BEGIN
    -- Add namespace column if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'namespace'
    ) THEN
        ALTER TABLE memories ADD COLUMN namespace TEXT NOT NULL DEFAULT 'legacy';
        ALTER TABLE memories ALTER COLUMN namespace DROP DEFAULT;
    END IF;

    -- Add metadata column if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'metadata'
    ) THEN
        ALTER TABLE memories ADD COLUMN metadata JSONB;
    END IF;

    -- Replace old UNIQUE(key, user_id) with UNIQUE(namespace, key, scope, user_id)
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
          AND conname = 'memories_key_user_id_key'
    ) THEN
        ALTER TABLE memories DROP CONSTRAINT memories_key_user_id_key;
    END IF;

    -- Create the 4-tuple unique constraint only if NO unique constraint
    -- exists yet on memories. Phase 4 (below) supersedes this with a
    -- 5-tuple constraint including project — never re-add the 4-tuple
    -- once Phase 4 has run.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT memories_namespace_key_scope_user_id_key
            UNIQUE (namespace, key, scope, user_id);
    END IF;
    -- Add owner column if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'owner'
    ) THEN
        ALTER TABLE memories ADD COLUMN owner TEXT;
    END IF;

    -- Backfill owner from metadata.principal where available
    UPDATE memories SET owner = metadata->>'principal'
    WHERE owner IS NULL AND metadata->>'principal' IS NOT NULL;

    -- MEM-8: custody is separable from authorship. `owner` records who WROTE
    -- the row and never changes; `custodian` records who currently holds
    -- destruction rights (NULL = the author). Estate transfer sets it.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'custodian'
    ) THEN
        ALTER TABLE memories ADD COLUMN custodian TEXT;
    END IF;

    -- Normalize inbox addresses to lowercase (case-insensitive addressing)
    UPDATE memories SET user_id = LOWER(user_id)
    WHERE scope = 'inbox' AND user_id != LOWER(user_id);

    -- ---- Phase 4: project as first-class column -----------------------------
    -- Add project column if missing
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memories' AND column_name = 'project'
    ) THEN
        ALTER TABLE memories ADD COLUMN project TEXT;
    END IF;

    -- DROP the old 4-tuple constraint BEFORE running backfill — the backfill
    -- can collapse multiple rows onto the same (ns, key, scope, user_id)
    -- because user_id is being moved into the new project column.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
          AND conname = 'memories_namespace_key_scope_user_id_key'
    ) THEN
        ALTER TABLE memories DROP CONSTRAINT memories_namespace_key_scope_user_id_key;
    END IF;

    -- Owner backfill: rows from claude-code MCP traffic were previously owned
    -- by the 'claude-code' agent principal; the MCP bridge now authenticates
    -- as the deployment's owner principal. Backfill old + NULL owners to it
    -- (claude-code namespace, non-inbox/user scopes only — don't touch ha or
    -- inbox).
    --
    -- ⚠️ THIS NAME IS FUNCTIONAL, NOT COSMETIC. A 2026-08-26 public-repo
    -- scrub rewrote this literal as if it were prose and pointed the backfill
    -- at a principal that does not exist — which would have stamped ownership
    -- of the legacy corpus to a non-existent owner and locked the real one out
    -- behind the OWN-1 gate. Caught before deploy. It is fed from settings so
    -- the tree carries no hardcoded identity and a rename cannot silently
    -- corrupt ownership again.
    UPDATE memories SET owner = '__OWNER_PRINCIPAL__'
    WHERE namespace = 'claude-code'
      AND scope IN ('shared', 'machine', 'project')
      AND (owner IS NULL OR owner = 'claude-code');

    -- Phase 4 backfill: for scope=project rows, move user_id (the project
    -- name) into the new project column, and set user_id to the owner
    -- (the person who wrote it). Only runs on rows that haven't been
    -- migrated yet (project IS NULL).
    UPDATE memories
    SET project = user_id,
        user_id = COALESCE(owner, 'unknown')
    WHERE scope = 'project' AND project IS NULL;

    -- Add token_lookup column if missing (indexed token auth; existing
    -- rows backfill lazily on their next successful scan-match). The index
    -- is created unconditionally after — covers fresh AND upgraded DBs.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'principals' AND column_name = 'token_lookup'
    ) THEN
        ALTER TABLE principals ADD COLUMN token_lookup TEXT;
    END IF;
    CREATE INDEX IF NOT EXISTS idx_principals_token_lookup
        ON principals (token_lookup) WHERE token_lookup IS NOT NULL;

    -- AUDIT-2: principals had created_at and nothing else, so "when did this
    -- token die" — the first question asked during the 2026-08-16 rotated-
    -- credential incident — was unanswerable from the store. Existing rows
    -- get created_at as their honest floor: we know they have not been
    -- touched since we started recording, and we must not invent a date we
    -- never observed.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'principals' AND column_name = 'updated_at'
    ) THEN
        ALTER TABLE principals ADD COLUMN updated_at TIMESTAMPTZ;
        UPDATE principals SET updated_at = created_at WHERE updated_at IS NULL;
    END IF;

    -- Add the new 5-tuple unique constraint (NULLS NOT DISTINCT so NULL
    -- projects collide with each other — required for back-compat with
    -- scope=machine/shared/user/inbox rows where project IS NULL).
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
          AND conname = 'memories_namespace_key_scope_user_id_project_key'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT memories_namespace_key_scope_user_id_project_key
            UNIQUE NULLS NOT DISTINCT (namespace, key, scope, user_id, project);
    END IF;
END $$;
"""


async def init_pool() -> asyncpg.Pool:
    global pool
    pool = await asyncpg.create_pool(
        dsn=settings.dsn,
        min_size=2,
        max_size=10,
        init=_init_connection,
    )
    async with pool.acquire() as conn:
        # SCHEMA_SQL first (CREATE TABLE IF NOT EXISTS — no-op on existing
        # DBs, creates current shape on fresh ones). MIGRATE_SQL then alters
        # in place to handle upgrades from older schemas.
        await conn.execute(SCHEMA_SQL)
        await conn.execute(_render_migrate_sql())
    return pool


def _render_migrate_sql() -> str:
    """MIGRATE_SQL with the owner-backfill target resolved from settings.

    The owner principal cannot be a bind parameter: MIGRATE_SQL is one
    multi-statement block, executed whole. So it is substituted — and
    VALIDATED first, because a name interpolated into SQL is an injection
    site if it is ever attacker- or typo-shaped.

    If no owner principal is configured, the backfill is REMOVED rather than
    run with a guess. Stamping the legacy corpus with a wrong owner would lock
    the real one out behind the OWN-1 gate, and only a restore would undo it —
    exactly the damage a 2026-08-26 scrub nearly caused by rewriting the
    literal as if it were prose. Doing nothing is recoverable; doing the wrong
    thing is not.
    """
    name = (settings.owner_principal_name or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", name):
        if name:
            logger.warning(
                "owner backfill SKIPPED: owner_principal_name %r is not a "
                "plain principal name; refusing to interpolate it into SQL",
                name,
            )
        else:
            logger.info(
                "owner backfill skipped: no owner_principal_name configured"
            )
        # Drop just the backfill statement, keep every other migration.
        return re.sub(
            r"\n\s*UPDATE memories SET owner = '__OWNER_PRINCIPAL__'.*?;",
            "", MIGRATE_SQL, flags=re.DOTALL,
        )
    return MIGRATE_SQL.replace("__OWNER_PRINCIPAL__", name)


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def close_pool() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


async def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    return pool
