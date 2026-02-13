"""Bootstrap auto-seed tests."""

import pytest
from unittest.mock import patch, AsyncMock

from server.services import principal_service as ps


async def _cleanup_principal(name: str):
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


@pytest.mark.asyncio
async def test_bootstrap_creates_admin_when_enforced(db_pool):
    """When require_auth=true and no admins exist, _bootstrap_admin creates one."""
    try:
        with patch("server.main.settings") as mock_settings:
            mock_settings.require_auth = True
            mock_settings.api_token = "bootstrap-test-token"

            from server.main import _bootstrap_admin
            await _bootstrap_admin()

            principal = await ps.get_principal("_bootstrap")
            assert principal is not None
            assert principal["is_admin"] is True
            assert principal["type"] == "agent"

            # Verify the token works
            found = await ps.get_principal_by_token("bootstrap-test-token")
            assert found is not None
            assert found["name"] == "_bootstrap"
    finally:
        await _cleanup_principal("_bootstrap")


@pytest.mark.asyncio
async def test_bootstrap_skips_when_admin_exists(db_pool):
    """When an admin already exists, _bootstrap_admin does nothing."""
    try:
        await ps.create_principal(
            name="existing-admin",
            type="agent",
            is_admin=True,
        )

        with patch("server.main.settings") as mock_settings:
            mock_settings.require_auth = True
            mock_settings.api_token = "should-not-be-used"

            from server.main import _bootstrap_admin
            await _bootstrap_admin()

            # _bootstrap should NOT have been created
            principal = await ps.get_principal("_bootstrap")
            assert principal is None
    finally:
        await _cleanup_principal("existing-admin")
        await _cleanup_principal("_bootstrap")


@pytest.mark.asyncio
async def test_bootstrap_idempotent(db_pool):
    """Running _bootstrap_admin twice doesn't error."""
    try:
        with patch("server.main.settings") as mock_settings:
            mock_settings.require_auth = True
            mock_settings.api_token = "idempotent-test-token"

            from server.main import _bootstrap_admin
            await _bootstrap_admin()
            # Run again — should catch UniqueViolation gracefully
            await _bootstrap_admin()

            principal = await ps.get_principal("_bootstrap")
            assert principal is not None
            assert principal["is_admin"] is True
    finally:
        await _cleanup_principal("_bootstrap")


@pytest.mark.asyncio
async def test_bootstrap_skips_when_not_enforced(db_pool):
    """When require_auth=false, _bootstrap_admin does nothing."""
    with patch("server.main.settings") as mock_settings:
        mock_settings.require_auth = False
        mock_settings.api_token = "some-token"

        from server.main import _bootstrap_admin
        await _bootstrap_admin()

        principal = await ps.get_principal("_bootstrap")
        assert principal is None


@pytest.mark.asyncio
async def test_bootstrap_skips_when_no_api_token(db_pool):
    """When no api_token is set, _bootstrap_admin does nothing."""
    with patch("server.main.settings") as mock_settings:
        mock_settings.require_auth = True
        mock_settings.api_token = ""

        from server.main import _bootstrap_admin
        await _bootstrap_admin()

        principal = await ps.get_principal("_bootstrap")
        assert principal is None
