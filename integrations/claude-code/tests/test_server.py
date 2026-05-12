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
async def test_memory_store_sends_provenance_and_identity(respx_mock):
    """Writes must include X-Engram-Project / X-Engram-Cwd headers and
    the listen_set + reader_identity on the request body so the server can
    attach an inbox banner and record the origin folder."""
    route = respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "k"})
    )
    with patch("engram_mcp.identity.hostname", return_value="macmini"):
        await memory_store(
            key="k",
            value="v",
            scope="project",
            project_dir="/Users/ixanadu/projects/engram",
        )
    req = route.calls.last.request
    assert req.headers["x-engram-project"] == "engram"
    assert req.headers["x-engram-cwd"] == "/Users/ixanadu/projects/engram"
    import json as _json
    body = _json.loads(req.content)
    assert body["reader_identity"] == "engram@macmini"
    assert body["listen_set"] == ["engram", "machine:macmini", "engram@macmini"]


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store_admin_sends_admin_provenance(respx_mock):
    """Admin sessions (home dir, system paths) get X-Engram-Project=admin
    and reader_identity=admin@host."""
    route = respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "k"})
    )
    with patch("engram_mcp.identity.hostname", return_value="macmini"):
        await memory_store(
            key="k",
            value="v",
            scope="shared",
            project_dir="/Users/ixanadu",
        )
    req = route.calls.last.request
    assert req.headers["x-engram-project"] == "admin"
    assert req.headers["x-engram-cwd"] == "/Users/ixanadu"
    import json as _json
    body = _json.loads(req.content)
    assert body["reader_identity"] == "admin@macmini"
    assert body["listen_set"] == ["admin", "machine:macmini", "admin@macmini"]


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store_renders_inbox_banner(respx_mock):
    """When the server returns an inbox_banner on /memory/set, memory_store
    must prepend it to the return value so write-heavy sessions see mail."""
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "key": "test-key",
                "inbox_banner": {
                    "unread_count": 2,
                    "preview": [
                        "admin@macmini → engram: restart needed",
                        "projgamma@macbook → engram: ping",
                    ],
                },
            },
        )
    )
    with patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_store(
            key="test-key",
            value="v",
            project_dir="/Users/ixanadu/projects/engram",
        )
    banner_idx = result.find("📬 INBOX")
    head_idx = result.find("Stored memory")
    assert banner_idx != -1
    assert head_idx != -1
    assert banner_idx < head_idx
    assert "restart needed" in result


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
        # Phase 4: project name moves to the project column; user_id is the
        # principal (or 'unknown' when the bridge is anonymous in tests).
        assert "scope: project" in result
        assert "user_id: unknown" in result
        assert "project: my-app" in result
