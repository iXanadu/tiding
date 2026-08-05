"""Test MCP tool functions directly (they're just async functions)."""

import json

import httpx
import pytest
import respx
from unittest.mock import patch

from engram_mcp.config import settings as cfg
from engram_mcp.server import (
    VERSION,
    memory_store,
    memory_search,
    memory_get,
    memory_forget,
    memory_status,
    memory_whoami,
    memory_declare_identity,
    _format_recency,
)


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store(respx_mock):
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "test-key"})
    )
    result = await memory_store(key="test-key", value="hello world", tags="test")
    assert "Stored memory 'test-key'" in result
    # No namespace in the mocked (older-server) response -> falls back to config
    assert "namespace: fleet" in result


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
                        "created_at": "2026-06-10T12:00:00+00:00",
                    }
                ],
            },
        )
    )
    result = await memory_search(query="favorite color")
    assert "color" in result
    assert "blue" in result
    assert "0.920" in result
    # recency surfaces inline so the reader can date the fact
    assert "📅 2026-06-10" in result


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
                    "created_at": "2026-06-10T12:00:00+00:00",
                },
            },
        )
    )
    result = await memory_get(key="test-key")
    assert "test-key" in result
    assert "the value" in result
    assert "Stored: 📅 2026-06-10" in result


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


@respx.mock(base_url="http://localhost:8920")
async def test_memory_store_admin_override_project_and_user_id(respx_mock):
    """Passing project= and user_id= overrides resolution — lets an admin
    write into another project's partition without curl. Skips
    ensure_project_identity entirely when both are given."""
    route = respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "x"})
    )
    result = await memory_store(
        key="x",
        value="cross-project admin write",
        scope="project",
        project="other-app",
        user_id="ixanadu",
    )
    assert "user_id: ixanadu" in result
    assert "project: other-app" in result
    body = route.calls.last.request.read()
    assert b'"user_id":"ixanadu"' in body
    assert b'"project":"other-app"' in body


@respx.mock(base_url="http://localhost:8920")
async def test_memory_search_admin_override(respx_mock):
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    await memory_search(
        query="anything", scope="project", project="other-app", user_id="ixanadu"
    )
    body = route.calls.last.request.read()
    assert b'"user_id":"ixanadu"' in body
    assert b'"project":"other-app"' in body


# --- memory_whoami -----------------------------------------------------------

@respx.mock(base_url="http://localhost:8920")
async def test_memory_whoami_shows_identity_and_namespaces(respx_mock):
    respx_mock.get("/whoami").mock(return_value=httpx.Response(200, json={
        "name": "claude-code", "type": "agent", "is_admin": False,
        "read_namespaces": ["claude-code", "beast"], "write_namespaces": ["claude-code"],
    }))
    respx_mock.get("/namespaces").mock(return_value=httpx.Response(200, json={
        "status": "ok", "read": ["claude-code", "claude-web", "grok", "beast"],
        "write": ["claude-code"],
    }))
    with patch.object(cfg, "memory_api_token", "engram_test"):
        out = await memory_whoami()
    assert "Principal: claude-code" in out
    assert "admin=False" in out
    # namespaces come from /namespaces (wildcard-expanded), not the raw /whoami list
    assert "Can READ namespaces:  claude-code, claude-web, grok, beast" in out
    assert "Can WRITE namespaces: claude-code" in out


async def test_memory_whoami_anonymous_when_no_token():
    with patch.object(cfg, "memory_api_token", ""):
        out = await memory_whoami()
    assert "Not authenticated" in out


@respx.mock(base_url="http://localhost:8920")
async def test_memory_whoami_flags_legacy_alias_namespace(respx_mock):
    """Configured namespace outside the token's write set -> alias warning.

    This is the exact confusion a live Grok session hit: config said
    'claude-code', the server canonicalizes to 'fleet', and whoami printed
    both without explanation."""
    respx_mock.get("/whoami").mock(return_value=httpx.Response(200, json={
        "name": "grok", "type": "agent", "is_admin": False,
        "read_namespaces": ["fleet"], "write_namespaces": ["fleet"],
    }))
    respx_mock.get("/namespaces").mock(return_value=httpx.Response(200, json={
        "status": "ok", "read": ["fleet", "grok"], "write": ["fleet"],
    }))
    with patch.object(cfg, "memory_api_token", "engram_test"), \
         patch.object(cfg, "memory_namespace", "claude-code"):
        out = await memory_whoami()
    assert "LEGACY ALIAS" in out
    assert "Config source:" in out
    assert "you don't pick namespaces" in out


@respx.mock(base_url="http://localhost:8920")
async def test_memory_whoami_no_alias_warning_when_canonical(respx_mock):
    respx_mock.get("/whoami").mock(return_value=httpx.Response(200, json={
        "name": "claude-code", "type": "agent", "is_admin": False,
        "read_namespaces": ["fleet"], "write_namespaces": ["fleet"],
    }))
    respx_mock.get("/namespaces").mock(return_value=httpx.Response(200, json={
        "status": "ok", "read": ["fleet"], "write": ["fleet"],
    }))
    with patch.object(cfg, "memory_api_token", "engram_test"):
        out = await memory_whoami()
    assert "LEGACY ALIAS" not in out


@respx.mock(base_url="http://localhost:8920")
async def test_store_head_prefers_server_canonical_namespace(respx_mock):
    """Server echoes the canonical namespace it wrote to; display uses it,
    not the possibly-legacy configured name."""
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "key": "k", "namespace": "fleet"}
        )
    )
    with patch.object(cfg, "memory_namespace", "claude-code"):
        result = await memory_store(key="k", value="v")
    assert "namespace: fleet" in result
    assert "namespace: claude-code" not in result


# --- perms-driven search -----------------------------------------------------

@respx.mock(base_url="http://localhost:8920")
async def test_search_omits_namespaces_when_read_list_empty(respx_mock):
    """Empty memory_read_namespaces => the bridge sends no namespace(s), so the
    server resolves the search from the token's read permissions."""
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    with patch.object(cfg, "memory_read_namespaces", ""):
        await memory_search(query="hello", scope="shared")
    body = json.loads(route.calls.last.request.content)
    assert "namespaces" not in body
    assert "namespace" not in body


@respx.mock(base_url="http://localhost:8920")
async def test_search_narrows_when_read_list_set(respx_mock):
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    with patch.object(cfg, "memory_read_namespaces", "claude-code,beast"):
        await memory_search(query="hello", scope="shared")
    body = json.loads(route.calls.last.request.content)
    assert body["namespaces"] == ["claude-code", "beast"]


# --- project reads span all writers (MEM-5) ---

@respx.mock(base_url="http://localhost:8920")
async def test_project_search_defaults_to_all_writers(respx_mock, tmp_path):
    """A project read must not be partitioned by whoever wrote each row.

    Project memory belongs to the project. Filtering reads by the writing
    principal left each agent reading only its own notes, and an empty result
    is indistinguishable from an empty project — so nobody could detect it.
    """
    (tmp_path / ".engram.cfg").write_text("project = spantest\n")
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    await memory_search(query="hello", scope="project", project_dir=str(tmp_path))
    body = json.loads(route.calls.last.request.content)
    assert body["user_id"] == "*"
    assert body["project"] == "spantest"


@respx.mock(base_url="http://localhost:8920")
async def test_explicit_user_id_still_pins_one_writer(respx_mock, tmp_path):
    """An explicit user_id is a deliberate narrowing and must be honored."""
    (tmp_path / ".engram.cfg").write_text("project = spantest\n")
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    await memory_search(query="hello", scope="project",
                        project_dir=str(tmp_path), user_id="grok")
    body = json.loads(route.calls.last.request.content)
    assert body["user_id"] == "grok"


@respx.mock(base_url="http://localhost:8920")
async def test_non_project_scope_keeps_its_partition(respx_mock):
    """shared/machine/user partitions are meaningful — never wildcarded."""
    route = respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(200, json={"status": "ok", "results": []})
    )
    await memory_search(query="hello", scope="shared")
    body = json.loads(route.calls.last.request.content)
    assert body["user_id"] != "*"


# --- _format_recency (read-defensive recency annotation) ---

from datetime import datetime, timedelta, timezone


def test_format_recency_blank_when_missing():
    assert _format_recency(None) == ""
    assert _format_recency("") == ""


def test_format_recency_bad_input_is_safe():
    # never raise into the formatter — unparseable input degrades to no annotation
    assert _format_recency("not-a-date") == ""
    assert _format_recency(12345) == ""


def test_format_recency_today():
    now = datetime.now(timezone.utc).isoformat()
    out = _format_recency(now)
    assert out.startswith("📅 ")
    assert "(today)" in out


def test_format_recency_relative_buckets():
    now = datetime.now(timezone.utc)
    assert "(3d ago)" in _format_recency((now - timedelta(days=3)).isoformat())
    assert "mo ago)" in _format_recency((now - timedelta(days=90)).isoformat())
    assert "y ago)" in _format_recency((now - timedelta(days=800)).isoformat())


def test_format_recency_shows_absolute_date():
    # the absolute date is the durable part — present regardless of clock skew
    assert "📅 2026-06-10" in _format_recency("2026-06-10T12:00:00+00:00")


def test_format_recency_naive_datetime_assumed_utc():
    # a tz-naive stamp must not raise; treated as UTC
    out = _format_recency("2026-06-10T12:00:00")
    assert "📅 2026-06-10" in out


async def test_declare_identity_rejects_sentinel(tmp_path):
    # A deploy label / placeholder can't be persisted as identity — else read
    # would reject it and the prompt would loop forever.
    result = await memory_declare_identity(project_dir=str(tmp_path), name="prod")
    assert "deploy label" in result.lower() or "placeholder" in result.lower()
    assert not (tmp_path / ".engram.cfg").exists()  # nothing written


async def test_declare_identity_allows_real_name(tmp_path):
    result = await memory_declare_identity(project_dir=str(tmp_path), name="my-app")
    assert "Declared project identity: my-app" in result
    assert (tmp_path / ".engram.cfg").exists()


async def test_declare_identity_allows_admin(tmp_path):
    # 'admin' is a real, intentional identity — declaration must succeed.
    result = await memory_declare_identity(project_dir=str(tmp_path), name="admin")
    assert "Declared project identity: admin" in result
    assert "project = admin" in (tmp_path / ".engram.cfg").read_text()


# --- seat-collision banner (nonce -> heartbeat -> STOP prompt) ---------------

@respx.mock(base_url="http://localhost:8920")
async def test_heartbeat_sends_nonce_and_collision_sets_banner(respx_mock):
    import engram_mcp.server as srv
    respx_mock.post("/memory/presence").mock(return_value=httpx.Response(200, json={
        "status": "ok", "identity": "x", "state": "running",
        "collision": {"live_sessions": 2, "providers": ["claude"]},
    }))
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "k"}))
    srv._last_heartbeat = 0.0  # force a beat
    old = srv._SEAT_COLLISION
    try:
        result = await memory_store(key="k", value="v")
        # nonce rode the heartbeat
        beat_payload = json.loads(
            [c for c in respx_mock.calls if "/memory/presence" in str(c.request.url)][-1].request.read())
        assert beat_payload["session_nonce"] == srv._SESSION_NONCE
        # STOP banner prepended to the tool result
        assert "SEAT COLLISION" in result
        assert "ENGRAM_INBOX_IDENTITY=" in result
    finally:
        srv._SEAT_COLLISION = old


@respx.mock(base_url="http://localhost:8920")
async def test_collision_clear_removes_banner(respx_mock):
    import engram_mcp.server as srv
    respx_mock.post("/memory/presence").mock(return_value=httpx.Response(200, json={
        "status": "ok", "identity": "x", "state": "running", "collision": None,
    }))
    respx_mock.post("/memory/set").mock(
        return_value=httpx.Response(200, json={"status": "ok", "key": "k"}))
    srv._last_heartbeat = 0.0
    srv._SEAT_COLLISION = {"live_sessions": 2, "providers": ["claude"]}  # was colliding
    result = await memory_store(key="k", value="v")
    assert "SEAT COLLISION" not in result  # cleared by the clean heartbeat


@pytest.mark.asyncio
async def test_seat_claim_refreshes_and_does_not_stop_after_the_first(monkeypatch):
    """The seat claim must RUN AGAIN, not once per session.

    A seat row's last_used_at is its liveness signal. Claiming once froze that
    timestamp at session start, so a running session's seat read as not-live
    after the live window and became RECLAIMABLE after the grace period — at
    which point a new session in the same project could take the address out
    from under it, putting two sessions on one seat. That is the collision
    seats exist to prevent, reintroduced by the mechanism meant to prevent it.
    Observed live 2026-07-24 (three alive sessions all reporting live=false).
    """
    from engram_mcp import server as srv

    monkeypatch.setattr(srv, "_SEAT_CLAIMED", False)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_FAILURES", 0)
    monkeypatch.setattr(srv, "resolve_session_key", lambda: "claude-testkey")
    monkeypatch.setattr(srv, "derive_project_name", lambda _d: "proj")
    monkeypatch.setattr(srv, "compute_identity", lambda _d: ("proj-claude@host", []))

    calls = []

    async def _fake_claim(**kw):
        calls.append(kw["session_key"])
        return {"seat": "proj-claude", "is_new": False}

    monkeypatch.setattr(srv._client, "session_claim", _fake_claim)

    await srv._claim_seat("/tmp/proj")
    await srv._claim_seat("/tmp/proj")
    await srv._claim_seat("/tmp/proj")

    assert len(calls) == 3, (
        "the claim must refresh on every heartbeat; claiming once lets a live "
        f"session's seat go stale and be reclaimed (got {len(calls)} calls)"
    )


@pytest.mark.asyncio
async def test_no_session_key_retries_instead_of_latching(monkeypatch):
    """BRIDGE-1: an unresolvable key is a failure to RETRY, not a life
    sentence. The old permanent latch meant one transient resolution failure
    silenced the claim path forever — and the server cannot tell "never
    claimed" from "not running"; both are an absence. The probe costs one
    process lookup per heartbeat, so retrying is nearly free."""
    from engram_mcp import server as srv

    monkeypatch.setattr(srv, "_SEAT_CLAIMED", False)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_FAILURES", 0)
    monkeypatch.setattr(srv, "_SEAT_LAST_CLAIM_ERROR", None)
    monkeypatch.setattr(srv, "resolve_session_key", lambda: None)

    calls = []

    async def _fake_claim(**kw):
        calls.append(kw)
        return {"seat": "proj-claude", "is_new": False}

    monkeypatch.setattr(srv._client, "session_claim", _fake_claim)

    await srv._claim_seat("/tmp/proj")
    await srv._claim_seat("/tmp/proj")
    assert calls == [], "with no session key there is nothing to claim yet"
    assert srv._SEAT_CLAIM_FAILURES == 2, "failures are counted, not latched"

    # the key becomes resolvable (env injected late, transient ps failure
    # cleared) — the next heartbeat must claim, not stay silenced
    monkeypatch.setattr(srv, "resolve_session_key", lambda: "claude-latekey")
    monkeypatch.setattr(srv, "derive_project_name", lambda _d: "proj")
    monkeypatch.setattr(srv, "compute_identity", lambda _d: ("proj-claude@host", []))
    await srv._claim_seat("/tmp/proj")
    assert len(calls) == 1, "a recovered key must resume claiming"
    assert srv._SEAT_CLAIM_FAILURES == 0, "success resets the streak"


@pytest.mark.asyncio
async def test_persistent_claim_failure_surfaces_once(monkeypatch):
    """BRIDGE-1's other half: the streak must SPEAK. Below the threshold,
    silence (blips are normal); at it, one banner; never a nag."""
    from engram_mcp import server as srv

    monkeypatch.setattr(srv, "_SEAT_CLAIMED", False)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_FAILURES", 0)
    monkeypatch.setattr(srv, "_SEAT_LAST_CLAIM_ERROR", None)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_BANNER_SHOWN", False)
    monkeypatch.setattr(srv, "resolve_session_key", lambda: "claude-k")
    monkeypatch.setattr(srv, "derive_project_name", lambda _d: "proj")
    monkeypatch.setattr(srv, "compute_identity", lambda _d: ("proj-claude@host", []))

    async def _broken_claim(**kw):
        raise RuntimeError("server exploded")

    monkeypatch.setattr(srv._client, "session_claim", _broken_claim)

    await srv._claim_seat("/tmp/proj")
    await srv._claim_seat("/tmp/proj")
    assert srv._seat_claim_health_banner() == "", (
        "two blips must not alarm anyone"
    )
    await srv._claim_seat("/tmp/proj")
    banner = srv._seat_claim_health_banner()
    assert "SEAT CLAIM FAILING" in banner
    assert "RuntimeError: server exploded" in banner, (
        "the bare except used to swallow the reason; the banner must carry it"
    )
    assert srv._seat_claim_health_banner() == "", "once means once"


@pytest.mark.asyncio
async def test_a_runtime_seat_is_declared_and_the_server_grant_keeps_it(
    monkeypatch, tmp_path
):
    """ID-2, fixed: the claim DECLARES the runtime seat instead of losing to
    continuity.

    The defect this test used to pin: a launcher-spawned session re-claims on
    every heartbeat, and the registry answered from its record keyed on
    session_key — so it handed back the OLD seat, `_claim_seat` saw granted
    != preferred, and overwrote the file the agent had just set. The runtime
    seat was reverted within one heartbeat, silently. (Observed live
    2026-07-26: a session took `<proj>-claude-opus5` 71s after a restart
    while the registry held `<proj>-claude`.)

    Now the claim carries runtime_seat=True, the server moves the
    registration and grants the runtime name, and the bridge leaves the file
    alone — bridge, watcher and registry agree on the seat the session is
    actually on.
    """
    from engram_mcp import identity, server as srv

    monkeypatch.setenv(identity.SEATS_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ENGRAM_SESSION_KEY", "claude-ab-proj")
    monkeypatch.setattr(srv, "_SEAT_CLAIMED", False)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_FAILURES", 0)

    # The agent deliberately re-seats itself mid-session.
    identity.take_seat("proj-claude-opus5")
    assert identity.current_seat() == "proj-claude-opus5"
    assert identity.read_seat_file() == "proj-claude-opus5"

    calls = []

    async def _fake_claim(**kw):
        calls.append(kw)
        # A post-ID-2 server registers the runtime seat and grants it.
        return {"seat": kw["preferred_seat"], "is_new": False,
                "renamed_from": "proj-claude"}

    monkeypatch.setattr(srv._client, "session_claim", _fake_claim)
    await srv._claim_seat(str(tmp_path))

    assert calls[0]["runtime_seat"] is True, (
        "the claim must DECLARE the runtime seat, or the server cannot "
        "distinguish a deliberate re-seat from launch config"
    )
    assert calls[0]["preferred_seat"] == "proj-claude-opus5"
    assert identity.read_seat_file() == "proj-claude-opus5", (
        "the heartbeat overwrote the seat the agent just took — the revert "
        "loop ID-2 closed"
    )
    assert srv._SEAT_REVERT_NOTICE is None
    identity.clear_seat()


@pytest.mark.asyncio
async def test_a_refused_runtime_seat_reverts_loudly_not_silently(
    monkeypatch, tmp_path
):
    """The other consistent outcome: when the server refuses the runtime name
    (held by another live session), the bridge reverts to the granted seat so
    bridge, watcher and registry agree — and SAYS SO via the banner. A silent
    split was the original defect; a silent revert would be its twin."""
    from engram_mcp import identity, server as srv

    monkeypatch.setenv(identity.SEATS_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ENGRAM_SESSION_KEY", "claude-ab-proj")
    monkeypatch.setattr(srv, "_SEAT_CLAIMED", False)
    monkeypatch.setattr(srv, "_SEAT_CLAIM_FAILURES", 0)
    monkeypatch.setattr(srv, "_SEAT_REVERT_NOTICE", None)

    identity.take_seat("proj-claude-opus5")

    async def _refusing_claim(**kw):
        return {"seat": "proj-claude", "is_new": False,
                "warning": "runtime seat 'proj-claude-opus5' is unavailable "
                           "(held by another session); still registered as "
                           "'proj-claude'."}

    monkeypatch.setattr(srv._client, "session_claim", _refusing_claim)
    await srv._claim_seat(str(tmp_path))

    assert identity.read_seat_file() == "proj-claude", (
        "on refusal the bridge must converge to the registered seat"
    )
    assert srv._SEAT_REVERT_NOTICE and "unavailable" in srv._SEAT_REVERT_NOTICE
    assert "RUNTIME SEAT NOT REGISTERED" in srv._seat_revert_banner()
    identity.clear_seat()


def test_inbox_render_carries_a_timestamp_and_age():
    """Durable messages have no liveness dimension unless the render adds one.

    "Standing by" from two days ago must not render identically to "standing
    by" from two minutes ago. An agent read twenty present-tense messages and
    divided work with a peer dead 42 hours; the server had always sent
    created_at/age_hours/is_stale and this render dropped all three.
    """
    from engram_mcp.server import _format_inbox_message

    stale = _format_inbox_message({
        "id": "inbox/x", "to": "me", "from_": "peer", "subject": "standing by",
        "body": "waiting on you", "created_at": "2026-07-25T02:13:57Z",
        "age_hours": 50.2, "is_stale": True,
    })
    assert "2026-07-25 02:13:57 UTC" in stale
    assert "2d ago" in stale
    assert "STALE" in stale

    fresh = _format_inbox_message({
        "id": "inbox/y", "to": "me", "from_": "peer", "subject": "standing by",
        "body": "waiting on you", "created_at": "2026-07-27T18:40:00Z",
        "age_hours": 0.5, "is_stale": False,
    })
    assert "30m ago" in fresh
    assert "STALE" not in fresh
    assert fresh != stale, "a 2-day-old message rendered identically to a fresh one"


@pytest.mark.asyncio
async def test_admin_fallback_is_announced_once_then_quiet(monkeypatch, tmp_path):
    """ID-1: a session that BECAME admin by fallthrough is told so — once.

    The fallback stays (home-dir and maintenance sessions share admin on
    purpose, and an unconfigured session must still heartbeat); the silence
    goes. One line on the first tool result, nothing after: a fact stated,
    not a nag. Sessions with an explicit identity override are not wearing
    admin's name and stay quiet.
    """
    from engram_mcp import identity, server as srv

    monkeypatch.setattr(srv, "_ADMIN_FALLBACK_ANNOUNCED", False)
    identity.reset_session_pin()
    identity.remember_project_dir(str(tmp_path))  # scratch dir → fallback admin
    try:
        first = srv._admin_fallback_banner()
        assert "shared 'admin' identity" in first
        assert "memory_declare_identity" in first, (
            "the banner must carry the fix, not just the fact"
        )
        assert srv._admin_fallback_banner() == "", "once means once"
    finally:
        identity.reset_session_pin()


@pytest.mark.asyncio
async def test_admin_fallback_banner_silent_for_project_and_seated_sessions(
    monkeypatch, tmp_path
):
    from engram_mcp import identity, server as srv

    # A pinned PROJECT session never sees it.
    monkeypatch.setattr(srv, "_ADMIN_FALLBACK_ANNOUNCED", False)
    identity.reset_session_pin()
    identity.remember_project_dir("/Users/x/projects/widget")
    try:
        assert srv._admin_fallback_banner() == ""
    finally:
        identity.reset_session_pin()

    # An admin-fallback dir WITH an explicit seat is precisely addressed —
    # it is not wearing admin's name, so nothing to announce.
    monkeypatch.setattr(srv, "_ADMIN_FALLBACK_ANNOUNCED", False)
    identity.remember_project_dir(str(tmp_path))
    identity.take_seat("scratch-probe")
    try:
        assert srv._admin_fallback_banner() == ""
    finally:
        identity.clear_seat()
        identity.reset_session_pin()
