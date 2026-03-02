"""Test MCP tool functions directly (they're just async functions)."""

import httpx
import respx
from unittest.mock import patch

from engram_mcp.server import (
    VERSION,
    memory_store,
    memory_search,
    memory_get,
    memory_forget,
    memory_status,
)


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store(respx_mock):
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "test-key"})
    )
    result = await memory_store(key="test-key", value="hello world", tags="test")
    assert "Stored memory 'test-key'" in result
    assert "namespace: claude-code" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_search_results(respx_mock):
    respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "results": [
                    {
                        "namespace": "claude-code",
                        "key": "color",
                        "value": "My favorite color is blue",
                        "scope": "machine",
                        "tags": "preference",
                        "tags_search": "",
                        "score": 0.92,
                    }
                ],
            },
        )
    )
    result = await memory_search(query="favorite color")
    assert "color" in result
    assert "blue" in result
    assert "0.920" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_search_empty(respx_mock):
    respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    result = await memory_search(query="nonexistent")
    assert result == "No memories found."


async def test_memory_search_empty_query():
    """Empty query string should return 'No memories found.' without hitting the API."""
    result = await memory_search(query="")
    assert result == "No memories found."


async def test_memory_search_whitespace_query():
    """Whitespace-only query should return 'No memories found.' without hitting the API."""
    result = await memory_search(query="   ")
    assert result == "No memories found."


@respx.mock(base_url="http://localhost:8920")
async def test_memory_get_found(respx_mock):
    respx_mock.post("/memory/get").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "memory": {
                    "namespace": "claude-code",
                    "key": "test-key",
                    "value": "the value",
                    "scope": "machine",
                    "tags": "tag1",
                    "tags_search": "",
                },
            },
        )
    )
    result = await memory_get(key="test-key")
    assert "test-key" in result
    assert "the value" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_get_not_found(respx_mock):
    respx_mock.post("/memory/get").mock(
        return_value=httpx.Response(200, json={"status": "not_found", "memory": None})
    )
    result = await memory_get(key="missing")
    assert "No memory found" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_forget_found(respx_mock):
    respx_mock.post("/memory/forget").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "test-key"})
    )
    result = await memory_forget(key="test-key")
    assert "Deleted memory" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_forget_not_found(respx_mock):
    respx_mock.post("/memory/forget").mock(
        return_value=httpx.Response(
            200, json={"status": "not_found", "key": "missing"}
        )
    )
    result = await memory_forget(key="missing")
    assert "No memory found" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_status_ok(respx_mock):
    respx_mock.get("/health").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "checks": {"postgres": True, "embeddings": True}},
        )
    )
    result = await memory_status()
    assert "Memory service: ok" in result
    assert f"Server version: {VERSION}" in result
    assert "postgres: ok" in result


async def test_memory_status_unreachable():
    """When the API is down, status should report unreachable."""
    with patch(
        "engram_mcp.server._client.health",
        side_effect=Exception("Connection refused"),
    ):
        result = await memory_status()
        assert "unreachable" in result
        assert f"Server version: {VERSION}" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store_shared_scope(respx_mock):
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "shared-note"})
    )
    result = await memory_store(
        key="shared-note", value="works on all machines", scope="shared"
    )
    assert "scope: shared" in result
    assert "user_id: global" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store_project_scope(respx_mock):
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "proj-note"})
    )
    with patch(
        "engram_mcp.scoping.os.getcwd",
        return_value="/Users/test/projects/my-app",
    ):
        result = await memory_store(
            key="proj-note", value="project context", scope="project"
        )
        assert "scope: project" in result
        assert "user_id: my-app" in result
