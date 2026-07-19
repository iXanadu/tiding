import httpx


async def test_store(mock_api, client):
    mock_api.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "test-key"})
    )
    result = await client.store(
        key="test-key",
        value="test value",
        namespace="claude-code",
        scope="machine",
        user_id="testhost",
    )
    assert result == {"status": "ok", "key": "test-key"}


async def test_get_found(mock_api, client):
    mock_api.post("/memory/get").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "memory": {
                    "namespace": "claude-code",
                    "key": "test-key",
                    "value": "test value",
                    "scope": "machine",
                    "user_id": "testhost",
                    "tags": "test",
                    "tags_search": "",
                },
            },
        )
    )
    result = await client.get(
        key="test-key",
        namespace="claude-code",
        scope="machine",
        user_id="testhost",
    )
    assert result["status"] == "ok"
    assert result["memory"]["value"] == "test value"
    assert result["memory"]["namespace"] == "claude-code"


async def test_get_not_found(mock_api, client):
    mock_api.post("/memory/get").mock(
        return_value=httpx.Response(200, json={"status": "not_found", "memory": None})
    )
    result = await client.get(
        key="missing",
        namespace="claude-code",
        scope="machine",
        user_id="testhost",
    )
    assert result["status"] == "not_found"


async def test_search(mock_api, client):
    mock_api.post("/memory/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "results": [
                    {
                        "namespace": "claude-code",
                        "key": "color",
                        "value": "blue",
                        "scope": "machine",
                        "tags": "pref",
                        "tags_search": "",
                        "score": 0.85,
                    }
                ],
            },
        )
    )
    result = await client.search(
        query="favorite color",
        namespace="claude-code",
        scope="machine",
        user_id="testhost",
    )
    assert result["status"] == "ok"
    assert len(result["results"]) == 1
    assert result["results"][0]["score"] == 0.85


async def test_forget(mock_api, client):
    mock_api.post("/memory/forget").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "test-key"})
    )
    result = await client.forget(
        key="test-key",
        namespace="claude-code",
        scope="machine",
        user_id="testhost",
    )
    assert result["status"] == "ok"


async def test_health(mock_api, client):
    mock_api.get("/health").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "checks": {"postgres": True, "embeddings": True}},
        )
    )
    result = await client.health()
    assert result["status"] == "ok"
    assert result["checks"]["postgres"] is True


async def test_whoami(mock_api, client):
    mock_api.get("/whoami").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "00000000-0000-0000-0000-000000000000",
                "name": "ixanadu",
                "type": "human",
                "is_admin": True,
                "has_token": True,
                "has_password": False,
                "read_namespaces": ["*"],
                "write_namespaces": ["*"],
                "active": True,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )
    )
    result = await client.whoami()
    assert result["name"] == "ixanadu"
    assert result["is_admin"] is True


async def test_store_with_project(mock_api, client):
    captured: dict = {}

    def _capture(request):
        import json as _json
        captured.update(_json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "key": "k"})

    mock_api.post("/memory/set").mock(side_effect=_capture)
    await client.store(
        key="k",
        value="v",
        namespace="claude-code",
        scope="project",
        user_id="ixanadu",
        project="engram",
    )
    assert captured["project"] == "engram"
    assert captured["user_id"] == "ixanadu"


def test_provenance_uses_anchor_when_project_dir_omitted(monkeypatch):
    # D-1: an omitted project_dir must report the session's real project (via
    # the startup-cwd anchor), not collapse to admin/empty.
    import engram_mcp.client as client_mod
    import engram_mcp.identity as identity
    from engram_mcp.client import MemoryClient

    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu/projects/ProjBeta")
    monkeypatch.setattr(
        client_mod,
        "derive_project_name",
        lambda d: "projbeta" if d == "/Users/ixanadu/projects/ProjBeta" else "admin",
    )
    c = MemoryClient("http://localhost:8920")
    headers = c._provenance_headers(None)
    assert headers["X-Engram-Project"] == "projbeta"
    assert headers["X-Engram-Cwd"] == "/Users/ixanadu/projects/ProjBeta"


def test_provenance_explicit_project_dir_still_wins(monkeypatch):
    import engram_mcp.client as client_mod
    import engram_mcp.identity as identity
    from engram_mcp.client import MemoryClient

    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu/projects/ProjBeta")
    monkeypatch.setattr(
        client_mod,
        "derive_project_name",
        lambda d: (d or "").rsplit("/", 1)[-1].lower() or "admin",
    )
    c = MemoryClient("http://localhost:8920")
    headers = c._provenance_headers("/Users/ixanadu/projects/engram")
    assert headers["X-Engram-Project"] == "engram"
    assert headers["X-Engram-Cwd"] == "/Users/ixanadu/projects/engram"
