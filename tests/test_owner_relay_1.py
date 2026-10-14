"""OWNER-RELAY-1: a principal that relays the OWNER's words from outside AB
(the emergency Telegram bot) reaches the owner's mail and nothing else.

The owner's own token is full store admin; putting it on a spoke for a bot
would make a leak cost the whole store. A relay principal instead:
- may call only inbox read/send/ack/resolve and the roster (route fence),
- may read only the owner's addresses,
- sends every message as the owner (relayed_from=<owner>, authority kept),
  so readers see the same VERIFIED OWNER [via relay] badge the huddle relay
  earns, and it can never speak as anyone else.
"""
import pytest

from server.config import settings
from server.services.memory_service import INBOX_NAMESPACE

RELAY = "or1-relay"


@pytest.fixture
async def relay(client, db_pool, monkeypatch):
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    monkeypatch.setattr(settings, "owner_relay_principals", RELAY)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", RELAY)
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND value LIKE 'or1-%'")
    r = await client.post("/admin/principals", json={
        "name": RELAY, "type": "agent",
        "read_namespaces": [INBOX_NAMESPACE],
        "write_namespaces": [INBOX_NAMESPACE],
    })
    assert r.status_code in (200, 201), r.text
    yield {"Authorization": f"Bearer {r.json()['raw_token']}"}
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", RELAY)
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND value LIKE 'or1-%'")


@pytest.mark.asyncio
async def test_relay_reads_only_the_owners_inbox(client, relay):
    r = await client.post("/memory/inbox", headers=relay,
                          json={"listen_set": ["owner", "owner@hosta"]})
    assert r.status_code == 200, r.text
    r = await client.post("/memory/inbox", headers=relay,
                          json={"listen_set": ["owner", "or1-someone"]})
    assert r.status_code == 403, r.text
    assert "or1-someone" in r.json()["detail"]


@pytest.mark.asyncio
async def test_relay_cannot_reach_the_rest_of_the_store(client, relay):
    for method, path, body in [
        ("POST", "/memory/search", {"query": "x", "namespace": INBOX_NAMESPACE}),
        ("POST", "/memory/get", {"key": "k", "namespace": INBOX_NAMESPACE}),
        ("POST", "/memory/set", {"key": "or1-k", "value": "v",
                                  "namespace": INBOX_NAMESPACE}),
        ("GET", "/admin/principals", None),
    ]:
        r = await client.request(method, path, headers=relay, json=body)
        assert r.status_code == 403, (path, r.status_code, r.text)
    r = await client.get("/health", headers=relay)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_relay_send_is_the_owners_word_and_only_the_owners(client, relay, db_pool):
    r = await client.post("/memory/send", headers=relay, json={
        "to": "or1-agent", "body": "or1-hello from telegram", "subject": "s",
        # a forged author is replaced, never honoured
        "relayed_from": "or1-somebody-else"})
    assert r.status_code == 200, r.text
    assert any("replaced with 'owner'" in w
               for w in (r.json().get("recipient_warnings") or [])), r.json()
    async with db_pool.acquire() as conn:
        md = await conn.fetchval(
            "SELECT metadata FROM memories WHERE scope='inbox' "
            "AND value LIKE 'or1-hello%'")
    import json as _j
    md = _j.loads(md) if isinstance(md, str) else md
    assert md["relayed_from"] == "owner"
    assert md["authority"] is True
    assert md["from_principal"] == RELAY
    assert md["from"] == "owner"


@pytest.mark.asyncio
async def test_ordinary_agent_still_cannot_claim_relayed_authorship(client, db_pool, monkeypatch):
    """RELAY-1 unchanged for everyone else: relayed_from from a non-admin,
    non-relay principal is dropped and carries no authority."""
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    monkeypatch.setattr(settings, "owner_relay_principals", RELAY)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = 'or1-plain'")
    r = await client.post("/admin/principals", json={
        "name": "or1-plain", "type": "agent",
        "read_namespaces": [INBOX_NAMESPACE], "write_namespaces": [INBOX_NAMESPACE]})
    h = {"Authorization": f"Bearer {r.json()['raw_token']}"}
    try:
        r = await client.post("/memory/send", headers=h, json={
            "to": "or1-agent", "body": "or1-plain-send", "subject": "s",
            "relayed_from": "owner"})
        assert r.status_code == 200, r.text
        async with db_pool.acquire() as conn:
            md = await conn.fetchval(
                "SELECT metadata FROM memories WHERE scope='inbox' "
                "AND value = 'or1-plain-send'")
        import json as _j
        md = _j.loads(md) if isinstance(md, str) else md
        assert not md.get("relayed_from")
        assert not md.get("authority")
        # and the plain agent is NOT fenced
        r = await client.post("/memory/search", headers=h,
                              json={"query": "x", "namespace": INBOX_NAMESPACE})
        assert r.status_code != 403
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM principals WHERE name = 'or1-plain'")
            await conn.execute(
                "DELETE FROM memories WHERE scope='inbox' AND value LIKE 'or1-%'")


def test_no_owner_means_no_relay(monkeypatch):
    from server.auth import is_owner_relay
    monkeypatch.setattr(settings, "owner_relay_principals", RELAY)
    monkeypatch.setattr(settings, "owner_principal_name", "")
    assert not is_owner_relay({"name": RELAY})
    monkeypatch.setattr(settings, "owner_principal_name", "owner")
    assert is_owner_relay({"name": RELAY})
    assert not is_owner_relay({"name": "someone"})
