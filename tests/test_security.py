"""SEC-1 secure-by-default: bind guard tests."""

import pytest

from server.main import check_bind_security


def test_loopback_tokenless_ok():
    # Personal default: loopback needs no auth
    for host in ("127.0.0.1", "localhost", "::1"):
        check_bind_security(host, False, "", False)


def test_nonloopback_without_auth_refuses():
    with pytest.raises(RuntimeError, match="REFUSING TO START"):
        check_bind_security("0.0.0.0", False, "", False)


def test_nonloopback_with_require_auth_ok():
    check_bind_security("0.0.0.0", True, "", False)


def test_nonloopback_with_api_token_ok():
    check_bind_security("0.0.0.0", False, "engram_tok", False)


def test_nonloopback_with_explicit_optout_ok():
    # Trusted-network (Tailscale) deliberate opt-out
    check_bind_security("0.0.0.0", False, "", True)


# --- NS-1 namespace canonicalization (provider-agnostic rename) ---

import pytest as _pytest

from server.config import canonical_namespace


def test_alias_canonicalizes():
    assert canonical_namespace("claude-code") == "fleet"
    assert canonical_namespace("fleet") == "fleet"
    assert canonical_namespace("grok") == "grok"
    assert canonical_namespace(None) is None


@_pytest.mark.asyncio
async def test_legacy_namespace_client_lands_in_canonical(client, db_pool):
    """An OLD client still sending namespace='claude-code' must read/write the
    canonical 'fleet' bucket — the rename is a relabel, never a break."""
    r = await client.post("/memory/set", json={
        "namespace": "claude-code", "key": "ns1-alias-probe",
        "value": "written via legacy name", "scope": "machine", "user_id": "t",
    })
    assert r.status_code == 200, r.text
    # Truth-in-display: the response tells the client where the write REALLY
    # landed (canonical), so bridges can show 'fleet' not the legacy name.
    assert r.json()["namespace"] == "fleet"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT namespace FROM memories WHERE key='ns1-alias-probe'")
    assert row["namespace"] == "fleet"          # stored canonical
    # readable via BOTH names
    for ns in ("fleet", "claude-code"):
        r = await client.post("/memory/get", json={
            "namespace": ns, "key": "ns1-alias-probe",
            "scope": "machine", "user_id": "t",
        })
        assert r.status_code == 200
        assert r.json()["memory"]["value"] == "written via legacy name"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key='ns1-alias-probe'")
