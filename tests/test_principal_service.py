"""Tests for principal service: CRUD, tokens, passwords, aliases."""

import pytest

from server.services import principal_service as ps


# --- Helpers ---

async def _cleanup_principal(name: str):
    """Delete principal by name (CASCADE removes aliases too)."""
    pool = await ps.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM principals WHERE name = $1", name)


# --- Hash helpers ---

@pytest.mark.asyncio
async def test_password_hash_roundtrip(db_pool):
    hashed = await ps._hash_password("secret123")
    assert await ps._check_password("secret123", hashed)
    assert not await ps._check_password("wrong", hashed)


@pytest.mark.asyncio
async def test_token_format_and_hash(db_pool):
    raw, hashed = await ps.generate_token()
    assert raw.startswith("engram_")
    assert len(raw) > 20
    import bcrypt
    assert bcrypt.checkpw(raw.encode(), hashed.encode())


# --- Principal CRUD ---

@pytest.mark.asyncio
async def test_create_agent_auto_token(db_pool):
    try:
        principal, raw_token = await ps.create_principal(
            name="test-agent-1", type="agent",
        )
        assert principal["name"] == "test-agent-1"
        assert principal["type"] == "agent"
        assert principal["has_token"] is True
        assert principal["active"] is True
        assert raw_token is not None
        assert raw_token.startswith("engram_")
    finally:
        await _cleanup_principal("test-agent-1")


@pytest.mark.asyncio
async def test_create_human_with_password(db_pool):
    try:
        principal, raw_token = await ps.create_principal(
            name="test-human-1", type="human", password="placeholder-pw-1",
        )
        assert principal["name"] == "test-human-1"
        assert principal["type"] == "human"
        assert principal["has_password"] is True
        assert principal["has_token"] is False
        assert raw_token is None
    finally:
        await _cleanup_principal("test-human-1")


@pytest.mark.asyncio
async def test_name_normalization(db_pool):
    try:
        principal, _ = await ps.create_principal(
            name="  TestUser  ", type="human",
        )
        assert principal["name"] == "testuser"
    finally:
        await _cleanup_principal("testuser")


@pytest.mark.asyncio
async def test_get_nonexistent(db_pool):
    result = await ps.get_principal("no-such-principal-xyz")
    assert result is None


@pytest.mark.asyncio
async def test_list_and_filter(db_pool):
    try:
        await ps.create_principal(name="test-list-agent", type="agent")
        await ps.create_principal(name="test-list-human", type="human")

        all_principals = await ps.list_principals()
        names = [p["name"] for p in all_principals]
        assert "test-list-agent" in names
        assert "test-list-human" in names

        agents_only = await ps.list_principals(type="agent")
        agent_names = [p["name"] for p in agents_only]
        assert "test-list-agent" in agent_names
        assert "test-list-human" not in agent_names

        humans_only = await ps.list_principals(type="human")
        human_names = [p["name"] for p in humans_only]
        assert "test-list-human" in human_names
        assert "test-list-agent" not in human_names
    finally:
        await _cleanup_principal("test-list-agent")
        await _cleanup_principal("test-list-human")


@pytest.mark.asyncio
async def test_update_principal(db_pool):
    try:
        await ps.create_principal(name="test-update", type="human")

        updated, _ = await ps.update_principal(
            "test-update",
            is_admin=True,
            read_namespaces=["fleet", "beast"],
            write_namespaces=["human"],
        )
        assert updated["is_admin"] is True
        assert updated["read_namespaces"] == ["fleet", "beast"]
        assert updated["write_namespaces"] == ["human"]
    finally:
        await _cleanup_principal("test-update")


@pytest.mark.asyncio
async def test_deactivate_principal(db_pool):
    try:
        await ps.create_principal(name="test-deactivate", type="agent")

        result = await ps.deactivate_principal("test-deactivate")
        assert result is True

        principal = await ps.get_principal("test-deactivate")
        assert principal["active"] is False

        # second deactivate returns False (already inactive)
        result2 = await ps.deactivate_principal("test-deactivate")
        assert result2 is False
    finally:
        await _cleanup_principal("test-deactivate")


# --- Token auth ---

@pytest.mark.asyncio
async def test_lookup_by_token(db_pool):
    try:
        _, raw_token = await ps.create_principal(name="test-token-lookup", type="agent")
        found = await ps.get_principal_by_token(raw_token)
        assert found is not None
        assert found["name"] == "test-token-lookup"
    finally:
        await _cleanup_principal("test-token-lookup")


@pytest.mark.asyncio
async def test_deactivated_token_rejected(db_pool):
    try:
        _, raw_token = await ps.create_principal(name="test-token-inactive", type="agent")
        await ps.deactivate_principal("test-token-inactive")

        found = await ps.get_principal_by_token(raw_token)
        assert found is None
    finally:
        await _cleanup_principal("test-token-inactive")


# --- Password auth ---

@pytest.mark.asyncio
async def test_verify_password(db_pool):
    try:
        await ps.create_principal(
            name="test-pw-verify", type="human", password="placeholder-pw-2",
        )
        result = await ps.verify_password("test-pw-verify", "correct-horse")
        assert result is not None
        assert result["name"] == "test-pw-verify"

        wrong = await ps.verify_password("test-pw-verify", "wrong-password")
        assert wrong is None
    finally:
        await _cleanup_principal("test-pw-verify")


# --- Aliases ---

@pytest.mark.asyncio
async def test_add_and_resolve_alias(db_pool):
    try:
        await ps.create_principal(name="test-alias-owner", type="human")
        alias_rec = await ps.add_alias("test-alias-owner", "some-uuid-123", "ha")
        assert alias_rec is not None
        assert alias_rec["alias"] == "some-uuid-123"
        assert alias_rec["source"] == "ha"

        resolved = await ps.resolve_alias("some-uuid-123", "ha")
        assert resolved is not None
        assert resolved["name"] == "test-alias-owner"
    finally:
        await _cleanup_principal("test-alias-owner")


@pytest.mark.asyncio
async def test_alias_nonexistent_principal(db_pool):
    result = await ps.add_alias("no-such-principal-xyz", "alias", "ha")
    assert result is None


@pytest.mark.asyncio
async def test_alias_not_found(db_pool):
    result = await ps.resolve_alias("no-such-alias-xyz", "ha")
    assert result is None


@pytest.mark.asyncio
async def test_multiple_aliases(db_pool):
    try:
        await ps.create_principal(name="test-multi-alias", type="human")
        await ps.add_alias("test-multi-alias", "uuid-1", "ha")
        await ps.add_alias("test-multi-alias", "user@email.com", "email")

        aliases = await ps.list_aliases("test-multi-alias")
        assert len(aliases) == 2

        r1 = await ps.resolve_alias("uuid-1", "ha")
        assert r1["name"] == "test-multi-alias"
        r2 = await ps.resolve_alias("user@email.com", "email")
        assert r2["name"] == "test-multi-alias"
    finally:
        await _cleanup_principal("test-multi-alias")


@pytest.mark.asyncio
async def test_remove_alias(db_pool):
    try:
        await ps.create_principal(name="test-rm-alias", type="human")
        await ps.add_alias("test-rm-alias", "to-remove", "ha")

        removed = await ps.remove_alias("to-remove", "ha")
        assert removed is True

        resolved = await ps.resolve_alias("to-remove", "ha")
        assert resolved is None

        # remove again returns False
        removed2 = await ps.remove_alias("to-remove", "ha")
        assert removed2 is False
    finally:
        await _cleanup_principal("test-rm-alias")


@pytest.mark.asyncio
async def test_alias_deactivated_rejected(db_pool):
    try:
        await ps.create_principal(name="test-alias-deact", type="human")
        await ps.add_alias("test-alias-deact", "deact-alias", "ha")
        await ps.deactivate_principal("test-alias-deact")

        resolved = await ps.resolve_alias("deact-alias", "ha")
        assert resolved is None
    finally:
        await _cleanup_principal("test-alias-deact")


@pytest.mark.asyncio
async def test_alias_reassignment(db_pool):
    try:
        await ps.create_principal(name="test-reassign-a", type="human")
        await ps.create_principal(name="test-reassign-b", type="human")

        await ps.add_alias("test-reassign-a", "shared-alias", "ha")
        resolved = await ps.resolve_alias("shared-alias", "ha")
        assert resolved["name"] == "test-reassign-a"

        # reassign to B
        await ps.add_alias("test-reassign-b", "shared-alias", "ha")
        resolved = await ps.resolve_alias("shared-alias", "ha")
        assert resolved["name"] == "test-reassign-b"
    finally:
        await _cleanup_principal("test-reassign-a")
        await _cleanup_principal("test-reassign-b")
