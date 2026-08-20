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


@_pytest.fixture
def legacy_alias():
    """The alias MECHANISM outlives the retired claude-code=fleet default
    (NS-2) — tests exercise it with an explicitly-set alias."""
    from server.config import settings as _settings
    prev = _settings.namespace_aliases
    _settings.namespace_aliases = "claude-code=fleet"
    yield
    _settings.namespace_aliases = prev


def test_alias_canonicalizes(legacy_alias):
    assert canonical_namespace("claude-code") == "fleet"
    assert canonical_namespace("fleet") == "fleet"
    assert canonical_namespace("grok") == "grok"
    assert canonical_namespace(None) is None


def test_no_alias_by_default():
    """NS-2: with the transition alias retired, names pass through untouched."""
    from server.config import settings as _settings
    assert _settings.namespace_aliases == "" or "claude-code" not in _settings.namespace_aliases
    assert canonical_namespace("fleet") == "fleet"


@_pytest.mark.asyncio
async def test_legacy_namespace_client_lands_in_canonical(client, db_pool, legacy_alias):
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


# --- SEC-9: an empty result must name the partition it searched -----------
# Three incidents on 2026-08-02, none permission-related: a read that omitted
# `project`; a first read after a token swap where user_id defaulted to the
# caller's own principal; a peer searching a project it had not written. In
# every case the query was well-formed, the caller authorised, and "0 hits"
# was indistinguishable from "the knowledge does not exist".

@pytest.mark.asyncio
async def test_zero_hit_search_states_the_partition_searched(client):
    r = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "sec9-nothing-can-possibly-match-this-zqxjv",
        "scope": "project",
        "user_id": "sec9-nobody",
        "project": "sec9-no-such-project",
        "limit": 5,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    warnings = " ".join(body.get("partition_warnings") or [])
    assert warnings, "a zero-hit search returned no partition advisory at all"
    # It must name WHERE it looked — that is the whole point.
    assert "scope=project" in warnings
    assert "sec9-nobody" in warnings
    assert "sec9-no-such-project" in warnings
    # And it must say what an empty answer does NOT mean.
    assert "not evidence" in warnings.lower()


@pytest.mark.asyncio
async def test_search_with_hits_carries_no_partition_advisory(client):
    """The advisory is for the ambiguous case only — noise on every hit would
    train readers to skip the line that matters."""
    await client.post("/memory/set", json={
        "namespace": "fleet", "key": "sec9/present",
        "value": "sec9 marker value that will match its own query",
        "scope": "user", "user_id": "sec9-present",
    })
    r = await client.post("/memory/search", json={
        "namespace": "fleet", "query": "sec9 marker value",
        "scope": "user", "user_id": "sec9-present", "limit": 5,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["results"], "precondition: this row needs a hit to be meaningful"
    assert not body.get("partition_warnings")
