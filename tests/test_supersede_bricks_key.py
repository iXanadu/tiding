"""SUPERSEDE-BRICKS-KEY-1: a superseded row must not permanently brick its key.

Measured (peer project 2026-08-28; mediaStudio reconfirmed 2026-09-08):
supersede retires a foreign-owned row in the caller's partition; default reads
return nothing; store then 409s ownership_conflict; forget is correctly REFUSED
(MEM-8 — do not loosen). Net: the handoff key is dead.

Fix shape (a): a drained row stops reserving its unique slot for ownership /
create-only. Hard-delete ownership stays strict.
"""

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from server.services import principal_service as ps


PROJ = "bricks-key-proj"
NS = "test"
KEY = "startup/next"


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


async def _cleanup_rows():
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE project = $1 AND key = $2", PROJ, KEY
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


def _row(value, user_id="claude-code", **kw):
    body = {
        "namespace": NS,
        "key": KEY,
        "value": value,
        "scope": "project",
        "project": PROJ,
        "user_id": user_id,
    }
    body.update(kw)
    return body


async def _seed_foreign_superseded(conn, owner="ixanadu", user_id="claude-code"):
    """The measured brick: same partition as the successor, foreign owner, drained."""
    await conn.execute(
        """
        INSERT INTO memories (namespace, key, value, scope, user_id, project, owner, metadata)
        VALUES ($1, $2, $3, 'project', $4, $5, $6, $7::jsonb)
        """,
        NS,
        KEY,
        "stale handoff from a departed identity",
        user_id,
        PROJ,
        owner,
        json.dumps({
            "status": "superseded",
            "superseded_at": "2026-04-11T00:00:00+00:00",
            "superseded_by_principal": "claude-code",
            "superseded_reason": "fixture: retired foreign-owned handoff",
        }),
    )


@pytest.mark.asyncio
async def test_store_reclaims_superseded_foreign_owned_slot(enforced_client):
    """mediaStudio shape: claude-code cannot write startup/next until reclaim."""
    try:
        _, tok = await ps.create_principal(
            name="bricks-claude", type="agent",
            write_namespaces=[NS], read_namespaces=[NS],
        )
        pool = await ps.get_pool()
        async with pool.acquire() as conn:
            await _seed_foreign_superseded(conn)

        resp = await enforced_client.post(
            "/memory/set",
            json=_row("fresh handoff", user_id="claude-code"),
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert resp.status_code == 200, resp.text

        got = await enforced_client.post(
            "/memory/get",
            json=_row("", user_id="claude-code"),
            headers={"Authorization": f"Bearer {tok}"},
        )
        body = got.json()
        assert body["status"] == "ok"
        assert body["memory"]["value"] == "fresh handoff"

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT owner, value, metadata->>'status' AS status
                FROM memories
                WHERE project = $1 AND key = $2 AND user_id = 'claude-code'
                """,
                PROJ,
                KEY,
            )
        assert row["owner"] == "bricks-claude"
        assert row["value"] == "fresh handoff"
        assert row["status"] is None or row["status"] not in (
            "superseded", "deletion_requested",
        )
    finally:
        await _cleanup_rows()
        await _cleanup_principal("bricks-claude")


@pytest.mark.asyncio
async def test_live_foreign_owner_still_409s(enforced_client):
    """Reclaim is only for drained rows — OWN-1 must still protect live peers."""
    try:
        _, tok_a = await ps.create_principal(
            name="bricks-live-a", type="agent",
            write_namespaces=[NS], read_namespaces=[NS],
        )
        _, tok_b = await ps.create_principal(
            name="bricks-live-b", type="agent",
            write_namespaces=[NS], read_namespaces=[NS],
        )
        assert (
            await enforced_client.post(
                "/memory/set",
                json=_row("A's live note", user_id="shared-partition"),
                headers={"Authorization": f"Bearer {tok_a}"},
            )
        ).status_code == 200

        resp = await enforced_client.post(
            "/memory/set",
            json=_row("B tries to clobber", user_id="shared-partition"),
            headers={"Authorization": f"Bearer {tok_b}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "ownership_conflict"
        assert resp.json()["detail"]["owner"] == "bricks-live-a"
    finally:
        await _cleanup_rows()
        await _cleanup_principal("bricks-live-a")
        await _cleanup_principal("bricks-live-b")


@pytest.mark.asyncio
async def test_forget_still_refuses_foreign_superseded(enforced_client):
    """MEM-8 unchanged: hard-delete stays controller-only even when drained."""
    try:
        _, tok = await ps.create_principal(
            name="bricks-forget", type="agent",
            write_namespaces=[NS], read_namespaces=[NS],
        )
        pool = await ps.get_pool()
        async with pool.acquire() as conn:
            await _seed_foreign_superseded(conn)

        resp = await enforced_client.post(
            "/memory/forget",
            json={
                "namespace": NS,
                "key": KEY,
                "scope": "project",
                "project": PROJ,
                "user_id": "claude-code",
            },
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert resp.status_code in (403, 409), resp.text
        detail = resp.json().get("detail") or {}
        if isinstance(detail, dict):
            msg = json.dumps(detail).lower()
        else:
            msg = str(detail).lower()
        assert "ixanadu" in msg or "control" in msg or "refus" in msg or "owner" in msg
    finally:
        await _cleanup_rows()
        await _cleanup_principal("bricks-forget")


@pytest.mark.asyncio
async def test_create_only_reclaims_drained_slot(enforced_client):
    """if_match='' must treat a superseded occupant as absent."""
    try:
        _, tok = await ps.create_principal(
            name="bricks-create", type="agent",
            write_namespaces=[NS], read_namespaces=[NS],
        )
        pool = await ps.get_pool()
        async with pool.acquire() as conn:
            await _seed_foreign_superseded(conn)

        resp = await enforced_client.post(
            "/memory/set",
            json=_row("create-only reclaim", user_id="claude-code", if_match=""),
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] is True
    finally:
        await _cleanup_rows()
        await _cleanup_principal("bricks-create")
