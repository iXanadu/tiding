"""MEM-8: destruction is self-only; flagging and estates are the escape valves.

The verb ladder under test (owner-specced 2026-08-16):
  1. supersede            — any writer, unchanged (covered in test_supersede.py)
  2. flag_deletion        — any namespace writer + reason; hides NOW, queues purge
  3. forget               — controller (custodian ?? owner) or admin only
  4. estate transfer      — admin reassigns custody; owner (authorship) immutable

Before this gate, /memory/forget checked only namespace write — and every dev
agent shares the namespace, so any of them could hard-delete any other's rows.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from server.services import principal_service as ps
from server.services.memory_service import get_pool


NS = "test"


async def _cleanup_principals(*names):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        for name in names:
            await conn.execute("DELETE FROM principals WHERE name = $1", name)


async def _cleanup_rows(prefix: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND key LIKE $2",
            NS, f"{prefix}%",
        )


@pytest_asyncio.fixture
async def enforced_client(services):
    with patch("server.auth.settings") as mock_settings, \
         patch("server.dependencies.settings") as mock_dep_settings:
        mock_settings.require_auth = True
        mock_settings.api_token = ""
        mock_dep_settings.require_auth = True
        mock_dep_settings.api_token = ""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c


@pytest_asyncio.fixture
async def two_writers(enforced_client):
    """Two non-admin writers plus one admin, all on the same namespace —
    the exact shape of the dev-agent fleet."""
    try:
        _, alice_tok = await ps.create_principal(
            name="mem8-alice", type="agent",
            read_namespaces=[NS], write_namespaces=[NS],
        )
        _, bob_tok = await ps.create_principal(
            name="mem8-bob", type="agent",
            read_namespaces=[NS], write_namespaces=[NS],
        )
        _, root_tok = await ps.create_principal(
            name="mem8-root", type="agent", is_admin=True,
            read_namespaces=[NS], write_namespaces=[NS],
        )
        yield {
            "alice": {"Authorization": f"Bearer {alice_tok}"},
            "bob": {"Authorization": f"Bearer {bob_tok}"},
            "root": {"Authorization": f"Bearer {root_tok}"},
        }
    finally:
        await _cleanup_principals("mem8-alice", "mem8-bob", "mem8-root")
        await _cleanup_rows("mem8-")


async def _store(client, headers, key, scope="shared", **kw):
    body = {"namespace": NS, "key": key, "value": f"value of {key}",
            "scope": scope, **kw}
    resp = await client.post("/memory/set", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp


# --- forget gate -----------------------------------------------------------

@pytest.mark.asyncio
async def test_peer_cannot_forget_another_writers_row(enforced_client, two_writers):
    """The headline rule: codex can no longer destroy grok's shared rows."""
    await _store(enforced_client, two_writers["alice"], "mem8-gate", "shared",
                 user_id="global")
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-gate", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["bob"])
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    # The refusal must name the controller and both escape valves.
    assert "mem8-alice" in detail
    assert "supersede" in detail
    assert "flag_deletion" in detail
    # And the row must still exist.
    resp = await enforced_client.post("/memory/get", json={
        "namespace": NS, "key": "mem8-gate", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["bob"])
    assert resp.status_code == 200
    assert resp.json()["memory"] is not None


@pytest.mark.asyncio
async def test_owner_can_forget_own_row(enforced_client, two_writers):
    await _store(enforced_client, two_writers["alice"], "mem8-own", "shared",
                 user_id="global")
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-own", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["alice"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_admin_bypasses_forget_gate(enforced_client, two_writers):
    await _store(enforced_client, two_writers["alice"], "mem8-admin", "shared",
                 user_id="global")
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-admin", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["root"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_legacy_null_owner_row_stays_deletable(enforced_client, two_writers):
    """Rows that predate attribution must not be locked away (OWN-1's rule)."""
    await _store(enforced_client, two_writers["alice"], "mem8-legacy", "shared",
                 user_id="global")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET owner = NULL WHERE namespace = $1 AND key = $2",
            NS, "mem8-legacy",
        )
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-legacy", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["bob"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- flag_deletion ---------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_hides_row_immediately_and_peer_can_flag(
    enforced_client, two_writers
):
    """A flagged secret's exposure window closes at flag time — and the
    flagger need not be the owner, only a namespace writer."""
    await _store(enforced_client, two_writers["alice"], "mem8-flag", "shared",
                 user_id="global")
    resp = await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-flag", "scope": "shared",
        "user_id": "global",
        "reason": "provider canceled — stored credential must be purged",
    }, headers=two_writers["bob"])
    assert resp.status_code == 200, resp.text
    # Hidden from default get...
    resp = await enforced_client.post("/memory/get", json={
        "namespace": NS, "key": "mem8-flag", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["alice"])
    assert resp.json()["memory"] is None
    # ...and from default search.
    resp = await enforced_client.post("/memory/search", json={
        "namespace": NS, "query": "value of mem8-flag", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["alice"])
    assert all(r["key"] != "mem8-flag" for r in resp.json()["results"])


@pytest.mark.asyncio
async def test_flag_requires_reason(enforced_client, two_writers):
    await _store(enforced_client, two_writers["alice"], "mem8-noreason", "shared",
                 user_id="global")
    resp = await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-noreason", "scope": "shared",
        "user_id": "global", "reason": "",
    }, headers=two_writers["bob"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_flag_is_not_restamped(enforced_client, two_writers):
    """First reason wins; the queue is the place to argue."""
    await _store(enforced_client, two_writers["alice"], "mem8-twice", "shared",
                 user_id="global")
    first = await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-twice", "scope": "shared",
        "user_id": "global", "reason": "first reason",
    }, headers=two_writers["alice"])
    assert first.status_code == 200
    second = await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-twice", "scope": "shared",
        "user_id": "global", "reason": "second reason",
    }, headers=two_writers["bob"])
    assert second.status_code == 404


# --- deletion queue --------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_lists_flag_and_admin_executes(enforced_client, two_writers):
    await _store(enforced_client, two_writers["alice"], "mem8-queue", "shared",
                 user_id="global")
    await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-queue", "scope": "shared",
        "user_id": "global", "reason": "purge me",
    }, headers=two_writers["bob"])
    resp = await enforced_client.get(
        "/admin/deletion-queue", headers=two_writers["root"]
    )
    assert resp.status_code == 200
    items = [i for i in resp.json()["items"] if i["key"] == "mem8-queue"]
    assert len(items) == 1
    assert items[0]["flagged_by"] == "mem8-bob"
    assert items[0]["reason"] == "purge me"
    assert items[0]["owner"] == "mem8-alice"
    # Execution IS an admin forget — no separate verb to learn.
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-queue", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["root"])
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_queue_reject_restores_row(enforced_client, two_writers):
    await _store(enforced_client, two_writers["alice"], "mem8-reject", "shared",
                 user_id="global")
    await enforced_client.post("/memory/flag_deletion", json={
        "namespace": NS, "key": "mem8-reject", "scope": "shared",
        "user_id": "global", "reason": "mistaken flag",
    }, headers=two_writers["bob"])
    resp = await enforced_client.post("/admin/deletion-queue/reject", json={
        "namespace": NS, "key": "mem8-reject", "scope": "shared",
        "user_id": "global", "reason": "content is fine — not a secret",
    }, headers=two_writers["root"])
    assert resp.status_code == 200, resp.text
    # The row is live again.
    resp = await enforced_client.post("/memory/get", json={
        "namespace": NS, "key": "mem8-reject", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["alice"])
    assert resp.json()["memory"] is not None


@pytest.mark.asyncio
async def test_flag_deletion_denied_without_write(enforced_client, two_writers):
    try:
        _, reader_tok = await ps.create_principal(
            name="mem8-reader", type="agent",
            read_namespaces=[NS], write_namespaces=[],
        )
        await _store(enforced_client, two_writers["alice"], "mem8-ro", "shared",
                     user_id="global")
        resp = await enforced_client.post("/memory/flag_deletion", json={
            "namespace": NS, "key": "mem8-ro", "scope": "shared",
            "user_id": "global", "reason": "should be refused",
        }, headers={"Authorization": f"Bearer {reader_tok}"})
        assert resp.status_code == 403
    finally:
        await _cleanup_principals("mem8-reader")


# --- estate transfer -------------------------------------------------------

@pytest.mark.asyncio
async def test_estate_transfer_moves_custody_keeps_authorship(
    enforced_client, two_writers
):
    """The abandoned-agent scenario: heir gains destruction rights, the
    departed writer's name stays on every row."""
    await _store(enforced_client, two_writers["alice"], "mem8-estate", "shared",
                 user_id="global")
    # Dry run first — counts, changes nothing.
    resp = await enforced_client.post("/admin/estate/transfer", json={
        "from_principal": "mem8-alice", "to_principal": "mem8-bob",
        "namespace": NS, "dry_run": True,
    }, headers=two_writers["root"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] >= 1
    assert resp.json()["dry_run"] is True
    # Bob still cannot delete before the real transfer.
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-estate", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["bob"])
    assert resp.status_code == 403
    # Execute.
    resp = await enforced_client.post("/admin/estate/transfer", json={
        "from_principal": "mem8-alice", "to_principal": "mem8-bob",
        "namespace": NS,
    }, headers=two_writers["root"])
    assert resp.status_code == 200
    assert resp.json()["rows"] >= 1
    # Authorship immutable, custody moved.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT owner, custodian FROM memories WHERE namespace=$1 AND key=$2",
            NS, "mem8-estate",
        )
    assert row["owner"] == "mem8-alice"
    assert row["custodian"] == "mem8-bob"
    # The heir can now delete; the author no longer can.
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-estate", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["alice"])
    assert resp.status_code == 403
    resp = await enforced_client.post("/memory/forget", json={
        "namespace": NS, "key": "mem8-estate", "scope": "shared",
        "user_id": "global",
    }, headers=two_writers["bob"])
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_estate_transfer_refuses_unknown_heir(enforced_client, two_writers):
    """A typo'd heir would strand destruction rights on a name that cannot
    authenticate — refused, not recorded."""
    resp = await enforced_client.post("/admin/estate/transfer", json={
        "from_principal": "mem8-alice", "to_principal": "mem8-nobody",
    }, headers=two_writers["root"])
    assert resp.status_code == 422
    assert "not a known principal" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_estate_transfer_requires_admin(enforced_client, two_writers):
    resp = await enforced_client.post("/admin/estate/transfer", json={
        "from_principal": "mem8-alice", "to_principal": "mem8-bob",
    }, headers=two_writers["bob"])
    assert resp.status_code == 403


# --- presence gate (PRES-1, rides this file's fixtures) --------------------

@pytest.mark.asyncio
async def test_fleet_read_only_principal_can_heartbeat_presence(enforced_client):
    """A messaging member (fleet READ) must be able to report its own
    liveness — send/ack/wait all work under the read gate, and presence was
    the lone write-gated outlier until the first read-only principal hit it."""
    try:
        _, tok = await ps.create_principal(
            name="mem8-reader-presence", type="agent",
            read_namespaces=[NS], write_namespaces=[],
        )
        with patch("server.routers.memory.INBOX_NAMESPACE", NS):
            resp = await enforced_client.post("/memory/presence", json={
                "identity": "mem8-reader-presence", "project": "mem8test",
                "state": "running", "provider": "grok",
            }, headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
    finally:
        await _cleanup_principals("mem8-reader-presence")
