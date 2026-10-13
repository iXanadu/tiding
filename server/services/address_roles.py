"""ROLE-1: owner-assigned roles — an address that one agent holds at a time.

The owner calls his chief-of-staff assistant "Hand" whatever product fills
the job; today that is the principal ``grokbot``, and one day it may be
another. A role is an address (``hand``) with exactly ONE current holder,
assigned only by an admin. Promoting a new assistant is one reassignment;
nobody's address book changes.

Semantics, chosen to match a chain of command:

* Mail to a role is STORED under the role (``user_id = 'hand'``), not under
  the holder. The role is a mailbox; the holder reads it. So after a handover
  the successor inherits the role's OPEN mail (its handover queue), threads
  already resolved stay resolved, and the previous holder keeps its own
  personal mail and loses only the role.
* The holder hears role mail by read-time listen expansion — server side,
  so a session that is already running picks up a role the moment it is
  assigned (the ``admin_listen_expansion`` lesson).
* Every assignment is kept in ``address_role_history``.

Why this is not the role-as-address the owner rejected on 2026-07-24: that
design let sessions CLAIM roles, so two agents asked to test both became
"engram-tester" and the address meant nobody in particular. Here a role has
one holder, set only by an admin, and no agent can claim one.
"""

from __future__ import annotations

import logging
import time

from server.config import settings
from server.db import get_pool

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0

_holder_of: dict[str, str] = {}   # role -> holder
_loaded_at: float = 0.0


def _norm(v: str | None) -> str:
    return (v or "").strip().lower()


async def refresh(force: bool = False) -> None:
    global _holder_of, _loaded_at
    if not force and _loaded_at and time.monotonic() - _loaded_at < CACHE_TTL_SECONDS:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT role, holder FROM address_roles")
    _holder_of = {r["role"]: r["holder"] for r in rows}
    _loaded_at = time.monotonic()


def holder(role: str | None) -> str | None:
    """Current holder of ``role`` (bare or ``role@host``), or None."""
    return _holder_of.get(_norm(role).partition("@")[0])


def roles_held_by(address: str | None) -> list[str]:
    base = _norm(address).partition("@")[0]
    return sorted(r for r, h in _holder_of.items() if h == base)


async def list_roles() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, holder, assigned_at, assigned_by FROM address_roles "
            "ORDER BY role")
    return [dict(r) for r in rows]


async def history(role: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, holder, assigned_at, assigned_by, ended_at "
            "FROM address_role_history WHERE role = $1 ORDER BY assigned_at",
            _norm(role))
    return [dict(r) for r in rows]


async def _refuse_role_name(conn, role: str) -> None:
    """A role name must not already mean something else — otherwise mail
    meant for that thing would be read by the role's holder."""
    from server.services.project_alias import PROJECT_NAME_RE, _aliases
    if not PROJECT_NAME_RE.match(role):
        raise ValueError(f"'{role}' is not a valid role name")
    reasons = []
    if role == _norm(settings.owner_principal_name):
        reasons.append("it is the owner's address")
    if await conn.fetchval(
            "SELECT 1 FROM principals WHERE lower(name) = $1 UNION "
            "SELECT 1 FROM principal_aliases WHERE lower(alias) = $1", role):
        reasons.append("it is a login (principal) name")
    if await conn.fetchval(
            "SELECT 1 FROM memories WHERE project = $1 LIMIT 1", role):
        reasons.append("it is a project with memory")
    if role in _aliases:
        reasons.append("it is an old name of a renamed project")
    if await conn.fetchval(
            "SELECT 1 FROM memories WHERE scope = 'presence' AND key = $1 "
            "LIMIT 1", f"presence/{role}"):
        reasons.append("a session is registered under it")
    if reasons:
        raise ValueError(f"refusing role '{role}': " + "; ".join(reasons))


async def assign(role: str, new_holder: str, actor: str | None = None) -> dict:
    """Give ``role`` to ``new_holder`` (first assignment or a handover)."""
    role = _norm(role)
    new_holder = _norm(new_holder)
    if not new_holder or "@" in new_holder:
        raise ValueError("holder must be a bare address, e.g. a principal name")
    await refresh(force=True)
    if new_holder in _holder_of:
        raise ValueError(f"'{new_holder}' is itself a role — roles do not chain")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT holder FROM address_roles WHERE role = $1 FOR UPDATE",
                role)
            if current is None:
                await _refuse_role_name(conn, role)
            elif current["holder"] == new_holder:
                return {"role": role, "holder": new_holder,
                        "previous": new_holder, "changed": False}
            await conn.execute(
                """
                INSERT INTO address_roles (role, holder, assigned_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (role) DO UPDATE
                SET holder = EXCLUDED.holder, assigned_by = EXCLUDED.assigned_by,
                    assigned_at = NOW()
                """, role, new_holder, actor)
            await conn.execute(
                "UPDATE address_role_history SET ended_at = NOW() "
                "WHERE role = $1 AND ended_at IS NULL", role)
            await conn.execute(
                "INSERT INTO address_role_history (role, holder, assigned_by) "
                "VALUES ($1, $2, $3)", role, new_holder, actor)
            known = await conn.fetchval(
                "SELECT active FROM principals WHERE lower(name) = $1",
                new_holder)
    await refresh(force=True)
    previous = current["holder"] if current else None
    logger.warning("ROLE %s: %s -> %s (by %s)", role, previous, new_holder, actor)
    return {"role": role, "holder": new_holder, "previous": previous,
            "changed": True,
            "note": None if known else (
                f"'{new_holder}' is not an active principal — fine for a "
                f"project or seat address, but check the spelling.")}


async def unassign(role: str, actor: str | None = None) -> bool:
    role = _norm(role)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            done = await conn.execute(
                "DELETE FROM address_roles WHERE role = $1", role)
            await conn.execute(
                "UPDATE address_role_history SET ended_at = NOW() "
                "WHERE role = $1 AND ended_at IS NULL", role)
    await refresh(force=True)
    logger.warning("ROLE %s retired (by %s)", role, actor)
    return done.endswith(" 1")
