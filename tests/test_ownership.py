"""OWN-1: a peer's project memory cannot be overwritten by claiming its user_id.

The owner's rule (2026-08-05): a development agent may READ any project memory
in the fleet, but may not CHANGE one another agent wrote — shared excepted.

Enforceable only because `owner` is recorded server-side from the token. The
client supplies `user_id` and can simply assert someone else's, which is
exactly how the defect was demonstrated: a 200, and a peer's value gone.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from server.services import principal_service as ps


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


async def _cleanup_rows(key: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key = $1", key)


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


def _row(key, **kw):
    base = {"namespace": "test", "key": key, "scope": "project",
            "project": "owntest", "user_id": "shared-partition"}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_peer_cannot_overwrite_by_claiming_user_id(enforced_client):
    """The measured defect: same user_id, different token, value replaced."""
    key = "own-test-peer-overwrite"
    try:
        _, tok_a = await ps.create_principal(
            name="own-writer-a", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        _, tok_b = await ps.create_principal(
            name="own-writer-b", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])

        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="written by A"),
            headers={"Authorization": f"Bearer {tok_a}"})
        assert resp.status_code == 200

        # B claims the SAME user_id — the exact spoof that used to return 200.
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="CLOBBERED by B"),
            headers={"Authorization": f"Bearer {tok_b}"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "ownership_conflict"
        # The refusal must NAME the holder — "denied" alone is indistinguishable
        # from a partition mistake.
        assert detail["owner"] == "own-writer-a"
        assert detail["attempted_by"] == "own-writer-b"

        # And A's value must still be there.
        resp = await enforced_client.post("/memory/get",
            json=_row(key), headers={"Authorization": f"Bearer {tok_a}"})
        assert resp.json()["memory"]["value"] == "written by A"
    finally:
        await _cleanup_rows(key)
        await _cleanup_principal("own-writer-a")
        await _cleanup_principal("own-writer-b")


@pytest.mark.asyncio
async def test_author_can_still_update_its_own_row(enforced_client):
    """Overwriting your OWN key is the normal case and must stay unimpeded."""
    key = "own-test-self-update"
    try:
        _, tok = await ps.create_principal(
            name="own-self", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        for value in ("first", "second", "third"):
            resp = await enforced_client.post("/memory/set",
                json=_row(key, value=value),
                headers={"Authorization": f"Bearer {tok}"})
            assert resp.status_code == 200
        resp = await enforced_client.post("/memory/get",
            json=_row(key), headers={"Authorization": f"Bearer {tok}"})
        assert resp.json()["memory"]["value"] == "third"
    finally:
        await _cleanup_rows(key)
        await _cleanup_principal("own-self")


@pytest.mark.asyncio
async def test_shared_scope_is_deliberately_everyones(enforced_client):
    """Owner's carve-out: 'unless it's shared'. shared stays mutable by anyone."""
    key = "own-test-shared-open"
    try:
        _, tok_a = await ps.create_principal(
            name="own-shared-a", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        _, tok_b = await ps.create_principal(
            name="own-shared-b", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        body = {"namespace": "test", "key": key, "scope": "shared",
                "user_id": "global"}
        resp = await enforced_client.post("/memory/set",
            json={**body, "value": "by A"},
            headers={"Authorization": f"Bearer {tok_a}"})
        assert resp.status_code == 200
        resp = await enforced_client.post("/memory/set",
            json={**body, "value": "amended by B"},
            headers={"Authorization": f"Bearer {tok_b}"})
        assert resp.status_code == 200, "shared must not be ownership-gated"
    finally:
        await _cleanup_rows(key)
        await _cleanup_principal("own-shared-a")
        await _cleanup_principal("own-shared-b")


@pytest.mark.asyncio
async def test_legacy_null_owner_row_is_writable_and_gets_stamped(enforced_client):
    """12,525 rows predate the column. Refusing them would lock the corpus.

    They are allowed through and the write stamps `owner`, so the corpus
    becomes protected as it is touched rather than needing a migration.
    """
    key = "own-test-legacy-null"
    try:
        _, tok = await ps.create_principal(
            name="own-legacy", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="seed"),
            headers={"Authorization": f"Bearer {tok}"})
        assert resp.status_code == 200

        # Simulate a pre-column row.
        pool = await ps.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE memories SET owner = NULL WHERE key = $1", key)

        _, tok_other = await ps.create_principal(
            name="own-legacy-other", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="adopted"),
            headers={"Authorization": f"Bearer {tok_other}"})
        assert resp.status_code == 200, "a NULL-owner row must not be locked away"

        async with pool.acquire() as conn:
            owner = await conn.fetchval("SELECT owner FROM memories WHERE key = $1", key)
        assert owner == "own-legacy-other", "the write must stamp ownership"

        # Now protected: a third principal is refused.
        _, tok_third = await ps.create_principal(
            name="own-legacy-third", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="nope"),
            headers={"Authorization": f"Bearer {tok_third}"})
        assert resp.status_code == 409
    finally:
        await _cleanup_rows(key)
        for n in ("own-legacy", "own-legacy-other", "own-legacy-third"):
            await _cleanup_principal(n)


@pytest.mark.asyncio
async def test_admin_may_override(enforced_client):
    """The human/admin principal must be able to repair a peer's row."""
    key = "own-test-admin-override"
    try:
        _, tok_a = await ps.create_principal(
            name="own-admin-victim", type="agent",
            write_namespaces=["test"], read_namespaces=["test"])
        # type=agent so a token is auto-generated (create_principal only mints
        # one for agents); is_admin is the flag the override actually checks.
        _, tok_admin = await ps.create_principal(
            name="own-admin-boss", type="agent", is_admin=True,
            write_namespaces=["*"], read_namespaces=["*"])
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="by agent"),
            headers={"Authorization": f"Bearer {tok_a}"})
        assert resp.status_code == 200
        resp = await enforced_client.post("/memory/set",
            json=_row(key, value="repaired by admin"),
            headers={"Authorization": f"Bearer {tok_admin}"})
        assert resp.status_code == 200, "admin must be able to override"
    finally:
        await _cleanup_rows(key)
        await _cleanup_principal("own-admin-victim")
        await _cleanup_principal("own-admin-boss")
