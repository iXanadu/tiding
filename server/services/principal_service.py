"""Principal management service: CRUD, token/password hashing, alias resolution."""

import asyncio
import hashlib
import secrets
from uuid import UUID

import bcrypt

from server.db import get_pool


# --- Hashing helpers ---

def _bcrypt_input(plaintext: str) -> bytes:
    """bcrypt silently truncates input at 72 bytes — pre-hash anything longer
    so the full secret participates. Inputs ≤72 bytes pass through unchanged,
    which keeps every existing stored hash valid."""
    raw = plaintext.encode()
    if len(raw) > 72:
        return hashlib.sha256(raw).hexdigest().encode()
    return raw


def _token_lookup(raw_token: str) -> str:
    """Deterministic indexed lookup key for a token (SHA-256 hex). The token
    itself is high-entropy, so the digest is not reversible/bruteforceable;
    bcrypt remains the stored verifier."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _hash_password_sync(plaintext: str) -> str:
    return bcrypt.hashpw(_bcrypt_input(plaintext), bcrypt.gensalt()).decode()


async def _hash_password(plaintext: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, plaintext)


def _check_password_sync(plaintext: str, hashed: str) -> bool:
    return bcrypt.checkpw(_bcrypt_input(plaintext), hashed.encode())


async def _check_password(plaintext: str, hashed: str) -> bool:
    return await asyncio.to_thread(_check_password_sync, plaintext, hashed)


async def _hash_token(raw_token: str) -> str:
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(_bcrypt_input(raw_token), bcrypt.gensalt()).decode()
    )


async def generate_token() -> tuple[str, str]:
    """Return (raw_token, bcrypt_hash). The raw token is shown once at creation."""
    raw = "engram_" + secrets.token_urlsafe(32)
    return raw, await _hash_token(raw)


# --- Row → dict helper ---

def _principal_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "type": row["type"],
        "is_admin": row["is_admin"],
        "has_token": row["token_hash"] is not None,
        "has_password": row["password_hash"] is not None,
        "read_namespaces": list(row["read_namespaces"]),
        "write_namespaces": list(row["write_namespaces"]),
        "active": row["active"],
        "created_at": row["created_at"],
        # AUDIT-2: surfaced, not just stored. A forensic column no endpoint
        # returns answers nobody's question.
        "updated_at": row["updated_at"] if "updated_at" in row else None,
    }


# --- Principal CRUD ---

async def create_principal(
    name: str,
    type: str,
    is_admin: bool = False,
    password: str | None = None,
    token: str | None = None,
    read_namespaces: list[str] | None = None,
    write_namespaces: list[str] | None = None,
) -> tuple[dict, str | None]:
    """Create a principal. Returns (principal_dict, raw_token_or_None).

    For agents, a token is auto-generated if none provided.
    """
    name = name.strip().lower()
    pool = await get_pool()

    token_hash = None
    raw_token = None
    if token:
        raw_token = token
        token_hash = await _hash_token(token)
    elif type == "agent":
        raw_token, token_hash = await generate_token()

    token_lookup = _token_lookup(raw_token) if raw_token else None
    password_hash = await _hash_password(password) if password else None

    rns = read_namespaces or []
    wns = write_namespaces or []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO principals (name, type, is_admin, token_hash, token_lookup,
                                    password_hash, read_namespaces, write_namespaces)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            name, type, is_admin, token_hash, token_lookup, password_hash, rns, wns,
        )
    return _principal_dict(row), raw_token


async def get_principal(name: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM principals WHERE name = $1", name)
    return _principal_dict(row) if row else None


async def get_principal_by_id(principal_id: UUID) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM principals WHERE id = $1", principal_id)
    return _principal_dict(row) if row else None


async def get_principal_by_token(raw_token: str) -> dict | None:
    """Resolve a token to its principal.

    Fast path: indexed token_lookup (SHA-256 of the raw token) narrows to one
    row, then bcrypt verifies. An unknown token costs one indexed miss instead
    of a bcrypt scan across every principal (auth-spray DoS from the
    2026-07-21 audit).

    Legacy path: rows created before token_lookup existed are scanned with
    bcrypt and backfilled on first successful match.
    """
    pool = await get_pool()
    lookup = _token_lookup(raw_token)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM principals WHERE active = TRUE AND token_lookup = $1",
            lookup,
        )
    if row:
        match = await asyncio.to_thread(
            bcrypt.checkpw, _bcrypt_input(raw_token), row["token_hash"].encode()
        )
        return _principal_dict(row) if match else None

    # Legacy rows (no lookup key yet): bcrypt-scan, backfill on match.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM principals
            WHERE active = TRUE AND token_hash IS NOT NULL AND token_lookup IS NULL
            """
        )
    for row in rows:
        match = await asyncio.to_thread(
            bcrypt.checkpw, _bcrypt_input(raw_token), row["token_hash"].encode()
        )
        if match:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE principals SET token_lookup = $1 WHERE id = $2",
                    lookup, row["id"],
                )
            return _principal_dict(row)
    return None


async def list_principals(
    type: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    pool = await get_pool()
    clauses = []
    params: list = []
    idx = 1

    if active_only:
        clauses.append(f"active = ${idx}")
        params.append(True)
        idx += 1

    if type:
        clauses.append(f"type = ${idx}")
        params.append(type)
        idx += 1

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM principals{where} ORDER BY name", *params)
    return [_principal_dict(r) for r in rows]


async def update_principal(
    name: str,
    is_admin: bool | None = None,
    password: str | None = None,
    token: str | None = None,
    read_namespaces: list[str] | None = None,
    write_namespaces: list[str] | None = None,
    active: bool | None = None,
) -> tuple[dict | None, str | None]:
    """Update non-None fields. Returns (updated_dict, raw_token_or_None)."""
    pool = await get_pool()
    sets: list[str] = []
    params: list = []
    idx = 1
    raw_token = None

    if is_admin is not None:
        sets.append(f"is_admin = ${idx}")
        params.append(is_admin)
        idx += 1
    if password is not None:
        sets.append(f"password_hash = ${idx}")
        params.append(await _hash_password(password))
        idx += 1
    if token is not None:
        raw_token = token
        sets.append(f"token_hash = ${idx}")
        params.append(await _hash_token(token))
        idx += 1
        sets.append(f"token_lookup = ${idx}")
        params.append(_token_lookup(token))
        idx += 1
    if read_namespaces is not None:
        sets.append(f"read_namespaces = ${idx}")
        params.append(read_namespaces)
        idx += 1
    if write_namespaces is not None:
        sets.append(f"write_namespaces = ${idx}")
        params.append(write_namespaces)
        idx += 1
    if active is not None:
        sets.append(f"active = ${idx}")
        params.append(active)
        idx += 1

    if not sets:
        return await get_principal(name), None

    # AUDIT-2: stamped here, not at the call sites, so no future mutation can
    # forget it. "When did this token die" must be answerable from the store.
    sets.append("updated_at = NOW()")
    params.append(name)
    sql = f"UPDATE principals SET {', '.join(sets)} WHERE name = ${idx} RETURNING *"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if not row:
        return None, None
    return _principal_dict(row), raw_token


async def deactivate_principal(name: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE principals SET active = FALSE, updated_at = NOW() "
            "WHERE name = $1 AND active = TRUE", name
        )
    return result == "UPDATE 1"


# --- Alias operations ---

async def add_alias(principal_name: str, alias: str, source: str) -> dict | None:
    """Add or reassign an alias. Returns alias dict or None if principal not found."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        principal = await conn.fetchrow(
            "SELECT id FROM principals WHERE name = $1", principal_name
        )
        if not principal:
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO principal_aliases (principal_id, alias, source)
            VALUES ($1, $2, $3)
            ON CONFLICT (alias, source) DO UPDATE SET principal_id = $1
            RETURNING *
            """,
            principal["id"], alias, source,
        )
    return {
        "id": str(row["id"]),
        "principal_id": str(row["principal_id"]),
        "alias": row["alias"],
        "source": row["source"],
    }


async def remove_alias(
    alias: str,
    source: str | None = None,
    principal_name: str | None = None,
) -> bool:
    """Remove an alias. When principal_name is given, only that principal's
    alias is deletable — the API path names a principal, and deleting some
    OTHER principal's alias through it is a hijack (2026-07-21 audit)."""
    pool = await get_pool()
    clauses = ["alias = $1"]
    params: list = [alias]
    idx = 2
    if source:
        clauses.append(f"source = ${idx}")
        params.append(source)
        idx += 1
    if principal_name:
        clauses.append(
            f"principal_id = (SELECT id FROM principals WHERE name = ${idx})"
        )
        params.append(principal_name)
        idx += 1
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM principal_aliases WHERE {' AND '.join(clauses)}", *params
        )
    # asyncpg returns "DELETE N"
    return not result.endswith("0")


async def resolve_alias(alias: str, source: str | None = None) -> dict | None:
    """Resolve an alias to its active principal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            row = await conn.fetchrow(
                """
                SELECT p.* FROM principals p
                JOIN principal_aliases a ON a.principal_id = p.id
                WHERE a.alias = $1 AND a.source = $2 AND p.active = TRUE
                """,
                alias, source,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT p.* FROM principals p
                JOIN principal_aliases a ON a.principal_id = p.id
                WHERE a.alias = $1 AND p.active = TRUE
                """,
                alias,
            )
    return _principal_dict(row) if row else None


async def list_aliases(principal_name: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.* FROM principal_aliases a
            JOIN principals p ON p.id = a.principal_id
            WHERE p.name = $1
            ORDER BY a.source, a.alias
            """,
            principal_name,
        )
    return [
        {
            "id": str(r["id"]),
            "principal_id": str(r["principal_id"]),
            "alias": r["alias"],
            "source": r["source"],
        }
        for r in rows
    ]


# --- Password verification ---

async def verify_password(name: str, password: str) -> dict | None:
    """Verify password for a principal. Returns principal dict or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM principals WHERE name = $1 AND active = TRUE",
            name,
        )
    if not row or not row["password_hash"]:
        return None
    if await _check_password(password, row["password_hash"]):
        return _principal_dict(row)
    return None
