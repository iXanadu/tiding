"""Principal management service: CRUD, token/password hashing, alias resolution."""

import asyncio
import secrets
from uuid import UUID

import bcrypt

from server.db import get_pool


# --- Hashing helpers ---

def _hash_password_sync(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


async def _hash_password(plaintext: str) -> str:
    return await asyncio.to_thread(_hash_password_sync, plaintext)


def _check_password_sync(plaintext: str, hashed: str) -> bool:
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


async def _check_password(plaintext: str, hashed: str) -> bool:
    return await asyncio.to_thread(_check_password_sync, plaintext, hashed)


async def generate_token() -> tuple[str, str]:
    """Return (raw_token, bcrypt_hash). The raw token is shown once at creation."""
    raw = "engram_" + secrets.token_urlsafe(32)
    hashed = await asyncio.to_thread(
        lambda: bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()
    )
    return raw, hashed


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
        token_hash = await asyncio.to_thread(
            lambda: bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()
        )
    elif type == "agent":
        raw_token, token_hash = await generate_token()

    password_hash = await _hash_password(password) if password else None

    rns = read_namespaces or []
    wns = write_namespaces or []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO principals (name, type, is_admin, token_hash, password_hash,
                                    read_namespaces, write_namespaces)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            name, type, is_admin, token_hash, password_hash, rns, wns,
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
    """Scan active principals and bcrypt-check the token. Fine at household scale."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM principals WHERE active = TRUE AND token_hash IS NOT NULL"
        )
    for row in rows:
        match = await asyncio.to_thread(
            bcrypt.checkpw, raw_token.encode(), row["token_hash"].encode()
        )
        if match:
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
        params.append(await asyncio.to_thread(
            lambda: bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()
        ))
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
            "UPDATE principals SET active = FALSE WHERE name = $1 AND active = TRUE", name
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


async def remove_alias(alias: str, source: str | None = None) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            result = await conn.execute(
                "DELETE FROM principal_aliases WHERE alias = $1 AND source = $2",
                alias, source,
            )
        else:
            result = await conn.execute(
                "DELETE FROM principal_aliases WHERE alias = $1", alias
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
