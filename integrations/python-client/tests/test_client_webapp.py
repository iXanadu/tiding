"""Tests for the web-app conveniences: from_env, enabled, is_available."""

import os

import pytest

from engram_client import EngramClient


def test_from_env_reads_prefixed_vars(monkeypatch):
    monkeypatch.setenv("BEASTCHAT_ENGRAM_URL", "http://example:8920")
    monkeypatch.setenv("BEASTCHAT_ENGRAM_TOKEN", "engram_tok")
    monkeypatch.setenv("BEASTCHAT_ENGRAM_NAMESPACE", "ixanadu")
    monkeypatch.setenv("BEASTCHAT_ENGRAM_ENABLED", "false")
    c = EngramClient.from_env("BEASTCHAT")
    assert c.url == "http://example:8920"
    assert c.token == "engram_tok"
    assert c.namespace == "ixanadu"
    assert c.scope == "user"  # web-app default differs from the dataclass default
    assert c.enabled is False


def test_from_env_defaults(monkeypatch):
    for k in list(os.environ):
        if k.startswith("MYAPP_ENGRAM_"):
            monkeypatch.delenv(k, raising=False)
    # case-insensitive prefix, trailing underscore tolerated
    c = EngramClient.from_env("myapp_")
    assert c.url == "http://localhost:8920"
    assert c.enabled is True
    assert c.scope == "user"
    assert c.namespace == ""


def test_from_env_overrides_win(monkeypatch):
    monkeypatch.setenv("X_ENGRAM_NAMESPACE", "from-env")
    c = EngramClient.from_env("X", namespace="override", enabled=False)
    assert c.namespace == "override"
    assert c.enabled is False


@pytest.mark.asyncio
async def test_is_available_false_when_disabled():
    c = EngramClient(url="http://127.0.0.1:1", token="", namespace="n", project="", enabled=False)
    assert await c.is_available() is False


@pytest.mark.asyncio
async def test_is_available_true_when_healthy(monkeypatch):
    c = EngramClient(url="http://x", token="", namespace="n", project="")

    async def fake_health():
        return {"status": "ok"}

    monkeypatch.setattr(c, "health", fake_health)
    assert await c.is_available() is True


@pytest.mark.asyncio
async def test_is_available_false_when_unreachable(monkeypatch):
    c = EngramClient(url="http://x", token="", namespace="n", project="")

    async def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(c, "health", boom)
    assert await c.is_available() is False


@pytest.mark.asyncio
async def test_namespaces_wraps_endpoint(monkeypatch):
    c = EngramClient(url="http://x", token="t", namespace="n", project="")

    async def fake_request(method, path, **kw):
        assert method == "GET" and path == "/namespaces"
        return {"status": "ok", "read": ["a", "b"], "write": ["a"]}

    monkeypatch.setattr(c, "_request", fake_request)
    out = await c.namespaces()
    assert out["read"] == ["a", "b"]
    assert out["write"] == ["a"]
