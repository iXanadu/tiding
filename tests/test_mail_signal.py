"""WEBPUSH-1 — the mail signal: a login-free "is there new mail?" URL.

The route is the first unauthenticated one under /memory/, so most of this
file is about what must NOT open up: neighbouring paths, other methods, keys
in logs, and any hint distinguishing a revoked key from a missing route.
"""
import logging
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import server.services.principal_service as ps
from server.db import get_pool
from server.services import mail_signal
from server.services.memory_service import (
    INBOX_SCOPE,
    inbox_ack,
    inbox_resolve,
    inbox_send,
)

AGENT = "sigtest-agent"
ADMIN = "sigtest-admin"


async def _cleanup():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = ANY($1)",
                           [AGENT, ADMIN])
        await conn.execute("DELETE FROM mail_signal WHERE address = $1", AGENT)
        await conn.execute(
            "DELETE FROM memories WHERE scope = $1 AND user_id = $2",
            INBOX_SCOPE, AGENT)
        await conn.execute(
            "DELETE FROM request_log WHERE path LIKE '/memory/signal%'")


@pytest_asyncio.fixture
async def env(services):
    """require_auth=true client + an admin token + an agent principal."""
    await _cleanup()
    mail_signal._last_poll.clear()
    _, admin_token = await ps.create_principal(name=ADMIN, type="agent",
                                               is_admin=True)
    await ps.create_principal(name=AGENT, type="agent",
                              read_namespaces=["fleet"])
    with patch("server.auth.settings") as mock_settings, \
         patch("server.dependencies.settings") as mock_dep_settings:
        mock_settings.require_auth = True
        mock_settings.api_token = ""
        mock_settings.public_proxy_header = ""
        mock_dep_settings.require_auth = True
        mock_dep_settings.api_token = ""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport,
                               base_url="http://localhost") as c:
            yield c, {"Authorization": f"Bearer {admin_token}"}
    mail_signal._last_poll.clear()
    await _cleanup()


async def _issue(client, admin_h, address=AGENT, principal=None):
    body = {"address": address}
    if principal:
        body["principal"] = principal
    r = await client.post("/admin/signal", json=body, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["path"]


@pytest.fixture
def no_rate_limit():
    with patch.object(mail_signal, "MIN_POLL_INTERVAL_S", 0.0):
        yield


# ── pure helpers ───────────────────────────────────────────────────────────


class TestPredicates:
    def test_exact_single_segment_only(self):
        assert mail_signal.is_signal_path("/memory/signal/sig_abc")
        for p in ("/memory/signal", "/memory/signal/", "/memory/signal/a/b",
                  "/memory/signalx/sig_abc", "/memory/search",
                  "/memory/signal/sig_abc/"):
            assert not mail_signal.is_signal_path(p), p

    def test_redact(self):
        assert mail_signal.redact_path("/memory/signal/sig_secret") == \
            "/memory/signal/<redacted>"
        assert mail_signal.redact_path("/memory/signal/sig_s?x=1") == \
            "/memory/signal/<redacted>"
        assert mail_signal.redact_path("/memory/search") == "/memory/search"

    def test_rate_limit(self):
        mail_signal._last_poll.clear()
        assert not mail_signal.rate_limited("h", now=100.0)
        assert mail_signal.rate_limited("h", now=101.0)
        assert not mail_signal.rate_limited("h", now=102.5)
        mail_signal._last_poll.clear()

    def test_access_log_filter_masks_key(self):
        from server.main import _RedactSignalKey
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", "/memory/signal/sig_SECRET", "1.1", 200),
            None)
        assert _RedactSignalKey().filter(rec) is True
        assert "sig_SECRET" not in rec.getMessage()
        assert "/memory/signal/<redacted>" in rec.getMessage()


# ── the public route ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSignalRoute:
    async def test_valid_key_needs_no_token_and_says_nothing_else(
            self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        await inbox_send(to=AGENT, body="BODY-CANARY", subject="SUBJ-CANARY",
                         from_="SENDER-CANARY", intent="action")
        r = await client.get(path)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        data = r.json()
        assert set(data) == {"v", "cursor", "pending", "latest_at"}
        assert data["pending"] == 1 and data["cursor"] != "0"
        for canary in ("BODY-CANARY", "SUBJ-CANARY", "SENDER-CANARY", "inbox/"):
            assert canary not in r.text

    async def test_trailing_whitespace_in_the_key_is_ignored(
            self, env, no_rate_limit):
        """A vault value with a trailing newline arrives as %0A."""
        client, admin_h = env
        path = await _issue(client, admin_h)
        for suffix in ("%0A", "%0D%0A", "%20"):
            r = await client.get(path + suffix)
            assert r.status_code == 200, (suffix, r.status_code)

    async def test_neighbours_still_require_a_token(self, env):
        client, admin_h = env
        path = await _issue(client, admin_h)
        for method, p in (("GET", "/memory/signal"),
                          ("GET", "/memory/signal/"),
                          ("GET", path + "/extra"),
                          ("POST", path),
                          ("POST", "/memory/search"),
                          ("POST", "/memory/inbox")):
            r = await client.request(method, p)
            assert r.status_code == 401, (method, p, r.status_code)

    async def test_every_failure_is_the_same_404(self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        unknown = await client.get("/memory/signal/sig_doesnotexist")
        no_prefix = await client.get("/memory/signal/notasignalkey")

        r = await client.delete(f"/admin/signal/{AGENT}", headers=admin_h)
        assert r.json()["revoked"] == 1
        revoked = await client.get(path)

        for r in (unknown, no_prefix, revoked):
            assert r.status_code == 404
            assert r.json() == {"detail": "Not Found"}

    async def test_deactivating_the_login_kills_the_signal(
            self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        assert (await client.get(path)).status_code == 200
        await ps.deactivate_principal(AGENT)
        assert (await client.get(path)).status_code == 404

    async def test_reissue_rotates(self, env, no_rate_limit):
        client, admin_h = env
        old = await _issue(client, admin_h)
        new = await _issue(client, admin_h)
        assert old != new
        assert (await client.get(old)).status_code == 404
        assert (await client.get(new)).status_code == 200

    async def test_second_poll_inside_the_window_is_429(self, env):
        client, admin_h = env
        path = await _issue(client, admin_h)
        assert (await client.get(path)).status_code == 200
        r = await client.get(path)
        assert r.status_code == 429
        assert r.headers.get("retry-after")

    async def test_poll_is_recorded_for_the_operator(self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        await client.get(path)
        await client.get(path)
        r = await client.get("/admin/signal", headers=admin_h)
        mine = [s for s in r.json()["signals"] if s["address"] == AGENT]
        assert len(mine) == 1
        assert mine[0]["poll_count"] == 2 and mine[0]["last_polled_at"]

    async def test_key_never_reaches_request_log(self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        await client.get(path)
        await client.post(path)  # the 401 path logs too
        pool = await get_pool()
        async with pool.acquire() as conn:
            leaked = await conn.fetchval(
                "SELECT count(*) FROM request_log WHERE path LIKE $1",
                "%" + path.rsplit("/", 1)[1] + "%")
            masked = await conn.fetchval(
                "SELECT count(*) FROM request_log WHERE path = $1",
                mail_signal.REDACTED_PATH)
        assert leaked == 0
        assert masked >= 2


# ── what moves the cursor ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCursorSemantics:
    async def test_fyi_and_self_mail_do_not_move_it(self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        before = (await client.get(path)).json()
        await inbox_send(to=AGENT, body="x", from_="someone", intent="fyi")
        await inbox_send(to=AGENT, body="x", from_=AGENT, intent="action")
        after = (await client.get(path)).json()
        assert after == before
        assert after["pending"] == 0 and after["cursor"] == "0"

    async def test_arrival_moves_it_ack_and_resolve_do_not(
            self, env, no_rate_limit):
        client, admin_h = env
        path = await _issue(client, admin_h)
        m1 = await inbox_send(to=AGENT, body="a", from_="peer", intent="action")
        s1 = (await client.get(path)).json()
        m2 = await inbox_send(to=AGENT, body="b", from_="peer")  # no intent wakes
        s2 = (await client.get(path)).json()
        assert s2["cursor"] != s1["cursor"]
        assert (s1["pending"], s2["pending"]) == (1, 2)

        await inbox_ack(m2, AGENT)
        s3 = (await client.get(path)).json()
        assert s3["cursor"] == s2["cursor"], "an ack must not trip the hook"
        assert s3["pending"] == 1

        await inbox_resolve(m1, AGENT)
        s4 = (await client.get(path)).json()
        assert s4["cursor"] == s2["cursor"]
        assert s4["pending"] == 0


# ── admin side ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAdmin:
    async def test_issue_requires_an_active_principal(self, env):
        client, admin_h = env
        r = await client.post("/admin/signal",
                              json={"address": "nobody-here"}, headers=admin_h)
        assert r.status_code == 422

    async def test_non_admin_cannot_issue(self, env):
        client, _ = env
        _, tok = await ps.create_principal(name=ADMIN + "-peer", type="agent",
                                           read_namespaces=["fleet"])
        try:
            r = await client.post("/admin/signal", json={"address": AGENT},
                                  headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403
        finally:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM principals WHERE name = $1",
                                   ADMIN + "-peer")

    async def test_raw_key_is_not_stored(self, env):
        client, admin_h = env
        path = await _issue(client, admin_h)
        raw = path.rsplit("/", 1)[1]
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM mail_signal WHERE address = $1 "
                "AND revoked_at IS NULL", AGENT)
        assert raw not in str(dict(row))
        assert row["key_hash"] == mail_signal.hash_key(raw)
