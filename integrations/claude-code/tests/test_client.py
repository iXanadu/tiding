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
