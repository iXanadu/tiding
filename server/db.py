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
    tags            TEXT NOT NULL DEFAULT '',
    tags_search     TEXT NOT NULL DEFAULT '',
    embedding       vector(768),
    search_text     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    UNIQUE (namespace, key, scope, user_id)
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
    password_hash       TEXT,
    read_namespaces     TEXT[] NOT NULL DEFAULT '{}',
    write_namespaces    TEXT[] NOT NULL DEFAULT '{}',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_principals_type ON principals (type);
CREATE INDEX IF NOT EXISTS idx_principals_active ON principals (id)
    WHERE active = TRUE;

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

    -- Replace old UNIQUE(key, user_id) with UNIQUE(namespace, key, scope, user_id)
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
          AND conname = 'memories_key_user_id_key'
    ) THEN
        ALTER TABLE memories DROP CONSTRAINT memories_key_user_id_key;
    END IF;

    -- Create new unique constraint if not exists
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'memories'::regclass
          AND contype = 'u'
          AND conname = 'memories_namespace_key_scope_user_id_key'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT memories_namespace_key_scope_user_id_key
            UNIQUE (namespace, key, scope, user_id);
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
        # Run migration first to add namespace column to existing tables,
        # then SCHEMA_SQL handles fresh installs (CREATE TABLE IF NOT EXISTS).
        await conn.execute(MIGRATE_SQL)
        await conn.execute(SCHEMA_SQL)
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
