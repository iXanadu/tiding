"""Adversarial tests for the 2026-07-21 security-audit hardening.

Each test is the proof for a specific audit finding: it would FAIL against
the pre-hardening code and passes now.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from server.main import app


@pytest.fixture
async def raw_client(services):
    """Client that can set an arbitrary Host header (to exercise the guard)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --- DNS rebinding: TrustedHostMiddleware -----------------------------------

@pytest.mark.asyncio
async def test_forged_host_header_rejected(raw_client):
    """A rebinding attacker's page sends Host: evil.com — must 400 before routing."""
    r = await raw_client.post("/memory/search",
                              json={"namespace": "t", "query": "x"},
                              headers={"host": "evil.com"})
    assert r.status_code == 400

@pytest.mark.asyncio
async def test_allowed_host_passes(raw_client):
    r = await raw_client.get("/health", headers={"host": "127.0.0.1"})
    assert r.status_code == 200


# --- reserved-scope guard on generic set/forget -----------------------------

@pytest.mark.asyncio
async def test_generic_set_cannot_write_inbox_scope(client):
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "key": "inbox/forged", "value": "tamper",
        "scope": "inbox", "user_id": "victim"})
    assert r.status_code == 400
    assert "own endpoints" in r.json()["detail"]

@pytest.mark.asyncio
async def test_generic_set_cannot_write_presence_scope(client):
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "key": "presence/victim", "value": "spoof",
        "scope": "presence", "user_id": "victim"})
    assert r.status_code == 400

@pytest.mark.asyncio
async def test_generic_forget_cannot_delete_inbox(client):
    r = await client.post("/memory/forget", json={
        "namespace": "fleet", "key": "inbox/whatever", "scope": "inbox",
        "user_id": "victim"})
    assert r.status_code == 400


# --- input-size caps (embedding-cost / row-flood DoS) -----------------------

@pytest.mark.asyncio
async def test_oversized_value_rejected(client):
    r = await client.post("/memory/set", json={
        "namespace": "t", "key": "k", "value": "A" * 300_000,
        "scope": "machine", "user_id": "u"})
    assert r.status_code == 422  # pydantic max_length

@pytest.mark.asyncio
async def test_search_limit_capped(client):
    r = await client.post("/memory/search", json={
        "namespace": "t", "query": "x", "limit": 10**8})
    assert r.status_code == 422

@pytest.mark.asyncio
async def test_negative_limit_rejected(client):
    r = await client.post("/memory/search", json={
        "namespace": "t", "query": "x", "limit": -1})
    assert r.status_code == 422


# --- bulk-delete LIKE-wildcard escape ---------------------------------------

@pytest.mark.asyncio
async def test_bulk_delete_wildcard_is_literal(client, db_pool):
    """key_prefix='%' must NOT wipe the namespace — it matches literal '%…' only."""
    async with db_pool.acquire() as conn:
        for k in ("keep-a", "keep-b", "%literal-pct"):
            await conn.execute(
                "INSERT INTO memories (namespace,key,value,scope,user_id,search_text,embedding) "
                "VALUES ('bdtest',$1,'v','machine','u',$1, (SELECT embedding FROM memories LIMIT 1)) "
                "ON CONFLICT DO NOTHING", k)
    r = await client.post("/admin/bulk-delete",
                          json={"namespace": "bdtest", "key_prefix": "%"})
    assert r.status_code == 200
    async with db_pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE namespace='bdtest'")
        # the two 'keep-*' rows survive; only the literal '%…' row could match
        assert remaining >= 2
        await conn.execute("DELETE FROM memories WHERE namespace='bdtest'")


# --- error detail does not leak internals -----------------------------------

@pytest.mark.asyncio
async def test_500_detail_is_generic(client, monkeypatch):
    import server.routers.memory as mem
    async def boom(*a, **k):
        raise RuntimeError("asyncpg: relation \"secret_table\" does not exist")
    monkeypatch.setattr(mem, "memory_get", boom)
    r = await client.post("/memory/get",
                          json={"namespace": "t", "key": "k", "scope": "machine", "user_id": "u"})
    assert r.status_code == 500
    assert "secret_table" not in r.text
    assert r.json()["detail"] == "internal error — see server logs"


# --- OBS-1: every uvicorn log line carries a date ---------------------------

def test_uvicorn_handlers_gain_a_timestamp_without_losing_their_formatter():
    """OBS-1: 840k+ access-log lines, not one of them datable.

    The fix prepends %(asctime)s to uvicorn's EXISTING formatters — including
    the AccessFormatter subclass that interpolates client/status fields —
    rather than replacing them, and must be idempotent (a reload must not
    stack timestamps).
    """
    import logging

    from uvicorn.logging import AccessFormatter, DefaultFormatter

    from server.main import timestamp_uvicorn_handlers

    access = logging.getLogger("uvicorn.access")
    default = logging.getLogger("uvicorn")
    saved = (access.handlers[:], default.handlers[:])
    try:
        # Reproduce uvicorn's own setup: its formatters, no asctime.
        ah = logging.StreamHandler()
        ah.setFormatter(AccessFormatter(
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" '
            "%(status_code)s", use_colors=False))
        access.handlers = [ah]
        dh = logging.StreamHandler()
        dh.setFormatter(DefaultFormatter(
            "%(levelprefix)s %(message)s", use_colors=False))
        default.handlers = [dh]

        timestamp_uvicorn_handlers()
        timestamp_uvicorn_handlers()  # idempotent — no double stamp

        assert ah.formatter._fmt.startswith("%(asctime)s ")
        assert ah.formatter._fmt.count("%(asctime)s") == 1
        assert dh.formatter._fmt.startswith("%(asctime)s ")

        # The stamped AccessFormatter still renders a real access record —
        # subclass behaviour (client/status interpolation) intact, date first.
        # uvicorn's access formatter unpacks args as a 5-tuple:
        # (client_addr, method, full_path, http_version, status_code)
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1234", "GET", "/health", "1.1", 200), None)
        line = ah.formatter.format(record)
        assert '127.0.0.1:1234 - "GET /health HTTP/1.1" 200' in line
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line), line
    finally:
        access.handlers, default.handlers = saved


# --- SEC-7 (locked "warn", 2026-07-27): typos are named, never swallowed ----

@pytest.mark.asyncio
async def test_unknown_fields_are_warned_about_by_name(client, db_pool):
    """A misspelled option used to vanish silently — the write proceeded
    without it and the caller debugged a guard that never ran. Rejecting
    would break shipped clients on a public API; warning names the typo at
    the first response read. Unanimous huddle vote, Rob locked W."""
    r = await client.post("/memory/set", json={
        "namespace": "sectest", "key": "warn/typo", "value": "v",
        "if_matched": "abc123",       # the canonical typo
        "expiry_days": 5,             # a second stray, also named
    })
    assert r.status_code == 200, "warn, not reject — the write must succeed"
    body = r.json()
    assert body["warning"] is not None
    assert "if_matched" in body["warning"]
    assert "expiry_days" in body["warning"]
    assert "if_match" in body["warning"], "the hint should point at the real field"
    assert body["if_match_applied"] is False, (
        "the typo'd guard must still honestly report it did not run"
    )

    clean = await client.post("/memory/set", json={
        "namespace": "sectest", "key": "warn/clean", "value": "v"})
    assert clean.json().get("warning") is None, "no stray fields, no warning"

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE namespace = 'sectest'")
        await conn.execute(
            "DELETE FROM audit_log WHERE detail::jsonb ->> 'namespace' = 'sectest'")
