"""WEBPUSH-1 — the mail signal: a login-free "is there new mail?" URL.

Built for agents hosted in someone else's cloud that can POLL a URL from a
hook but cannot be called and cannot hand their engram token to that hook
(measured on consumer Muse, 2026-10-09). The agent's hook polls

    GET /memory/signal/<key>  ->  {"v": 1, "cursor", "pending", "latest_at"}

and starts a run when ``cursor`` changes; the run then reads mail normally,
with the agent's own token. The signal deliberately carries no body, no
subject and no sender: a leaked key reveals only WHEN mail arrives and HOW
MUCH is waiting, and one admin call revokes it.

Two semantics worth knowing before changing anything here:

* ``cursor`` moves on ARRIVAL only. It is derived from the newest waking
  message ever addressed to the signal's address, regardless of whether it
  has since been read or resolved. If it moved on ack, the agent acking its
  mail would itself trip the hook and buy a pointless run — a paid turn per
  ack, on a receiver that pays per turn.
* "waking" means what it means for the long-poll (``/memory/inbox/wait``):
  not ``fyi``, not archived, and not sent by the address itself. ``pending``
  additionally requires open + not yet acked by the address, mirroring
  ``inbox_unread_count`` so the number agrees with what the agent will see.
"""
import hashlib
import secrets
import time

from server.db import get_pool
from server.services.memory_service import (
    INBOX_NAMESPACE,
    INBOX_OPEN,
    INBOX_SCOPE,
)

SIGNAL_PATH_PREFIX = "/memory/signal/"
REDACTED_PATH = SIGNAL_PATH_PREFIX + "<redacted>"
KEY_PREFIX = "sig_"

# One poll per key per this many seconds; a hook is told to poll every
# 10-15s, so this only ever bites a runaway loop or someone hammering a key.
MIN_POLL_INTERVAL_S = 2.0
_last_poll: dict[str, float] = {}


def is_signal_path(path: str) -> bool:
    """True for exactly ``/memory/signal/<one segment>`` and nothing else.

    This is the predicate the auth middleware exempts, so it is EXACT on
    purpose: no trailing slash, no sub-path, no empty key. Anything it
    rejects falls through to normal bearer auth and 401s.
    """
    if not path.startswith(SIGNAL_PATH_PREFIX):
        return False
    rest = path[len(SIGNAL_PATH_PREFIX):]
    return bool(rest) and "/" not in rest


def redact_path(path: str) -> str:
    """The path as it may be LOGGED: a signal key never reaches a log line."""
    if path.startswith(SIGNAL_PATH_PREFIX):
        return REDACTED_PATH
    return path


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def rate_limited(key_hash: str, now: float | None = None) -> bool:
    """True when this key polled less than MIN_POLL_INTERVAL_S ago.

    In-process on purpose: prod runs one uvicorn worker, and a limiter that
    resets on restart costs nothing here — the worst case is one extra poll.
    """
    now = time.monotonic() if now is None else now
    last = _last_poll.get(key_hash)
    if last is not None and now - last < MIN_POLL_INTERVAL_S:
        return True
    _last_poll[key_hash] = now
    return False


def _row(r) -> dict:
    return {
        "id": r["id"],
        "address": r["address"],
        "principal": r["principal"],
        "issued_by": r["issued_by"],
        "created_at": r["created_at"],
        "revoked_at": r["revoked_at"],
        "last_polled_at": r["last_polled_at"],
        "poll_count": r["poll_count"],
    }


async def issue_signal(
    address: str, principal: str, issued_by: str | None,
) -> tuple[dict, str]:
    """Issue a new key for ``address``, revoking any live one (rotation).

    Returns (row, raw_key). The raw key exists only in this return value.
    """
    address = address.strip().lower()
    raw_key = generate_key()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE mail_signal SET revoked_at = NOW() "
                "WHERE address = $1 AND revoked_at IS NULL",
                address,
            )
            r = await conn.fetchrow(
                """
                INSERT INTO mail_signal (address, principal, key_hash, issued_by)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                address, principal.strip().lower(), hash_key(raw_key), issued_by,
            )
    return _row(r), raw_key


async def revoke_signal(address: str) -> int:
    """Revoke every live key for ``address``. Returns how many were live."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE mail_signal SET revoked_at = NOW() "
            "WHERE address = $1 AND revoked_at IS NULL",
            address.strip().lower(),
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def list_signals(include_revoked: bool = False) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM mail_signal "
            "WHERE ($1 OR revoked_at IS NULL) "
            "ORDER BY address, created_at DESC",
            include_revoked,
        )
    return [_row(r) for r in rows]


async def lookup_signal(raw_key: str) -> dict | None:
    """Resolve a raw key to its live signal row and record the poll.

    None for an unknown key, a revoked key, or a key whose principal has been
    deactivated or deleted — deactivating the agent's login kills its signal
    too, with no second step to forget. The caller turns every None into the
    same 404, so the response never says which of those it was.
    """
    if not raw_key.startswith(KEY_PREFIX):
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            UPDATE mail_signal s
            SET last_polled_at = NOW(), poll_count = s.poll_count + 1
            FROM principals p
            WHERE s.key_hash = $1
              AND s.revoked_at IS NULL
              AND p.name = s.principal
              AND p.active
            RETURNING s.*
            """,
            hash_key(raw_key),
        )
    return _row(r) if r else None


async def signal_state(address: str) -> dict:
    """``{cursor, pending, latest_at}`` for one address. See module docstring."""
    address = address.strip().lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            WITH waking AS (
                SELECT key, created_at, metadata
                FROM memories
                WHERE namespace = $1
                  AND scope = $2
                  AND user_id = $3
                  AND COALESCE((metadata->>'archived')::bool, false) = false
                  AND COALESCE(metadata->>'intent', '') <> 'fyi'
                  AND lower(COALESCE(metadata->>'from', '')) <> $3
            )
            SELECT
                (SELECT key FROM waking
                 ORDER BY created_at DESC, key DESC LIMIT 1) AS latest_key,
                (SELECT max(created_at) FROM waking) AS latest_at,
                (SELECT count(*) FROM waking
                 WHERE COALESCE(metadata->>'status', $4) = $4
                   AND NOT COALESCE(metadata->'read_by', '[]'::jsonb) ? $3
                ) AS pending
            """,
            INBOX_NAMESPACE, INBOX_SCOPE, address, INBOX_OPEN,
        )
    latest_key = r["latest_key"]
    # Opaque on purpose: the agent only compares it for equality, and a raw
    # message id would hand a key-holder something to probe the API with.
    cursor = (
        hashlib.sha256(f"{address}|{latest_key}".encode()).hexdigest()[:16]
        if latest_key else "0"
    )
    return {
        "v": 1,
        "cursor": cursor,
        "pending": int(r["pending"] or 0),
        "latest_at": r["latest_at"].isoformat() if r["latest_at"] else None,
    }
