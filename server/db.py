import asyncpg
from pgvector.asyncpg import register_vector

from server.config import settings

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
"""

# Migration: add namespace column to tables created before this column existed.
MIGRATE_SQL = """
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
    -- as 'ixanadu'. Backfill old + NULL owners to ixanadu (claude-code
    -- namespace, non-inbox/user scopes only — don't touch ha or inbox).
    UPDATE memories SET owner = 'ixanadu'
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
        await conn.execute(MIGRATE_SQL)
    return pool


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
