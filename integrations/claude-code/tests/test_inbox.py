"""Tests for the MCP inbox tools."""

import httpx
import respx
from unittest.mock import patch

from engram_mcp.identity import compute_identity, is_admin_context
from engram_mcp.server import (
    _render_inbox_banner,
    memory_ack,
    memory_inbox,
    memory_inbox_archive,
    memory_reply,
    memory_search,
    memory_send,
)


# --- identity resolution ---

def test_is_admin_when_cwd_is_home():
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}):
        assert is_admin_context("/Users/ixanadu") is True
        assert is_admin_context(None) is True
        assert is_admin_context("") is True


def test_is_admin_false_for_project():
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}):
        assert is_admin_context("/Users/ixanadu/projects/engram") is False


def test_compute_identity_project():
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        reader, listen_set = compute_identity("/Users/ixanadu/projects/engram")
    assert reader == "engram@macmini"
    assert listen_set == ["engram", "machine:macmini"]


def test_compute_identity_admin():
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        reader, listen_set = compute_identity("/Users/ixanadu")
    assert reader == "machine:macmini"
    assert listen_set == ["machine:macmini"]


# --- banner rendering ---

def test_render_banner_none():
    assert _render_inbox_banner(None) == ""
    assert _render_inbox_banner({"unread_count": 0, "preview": []}) == ""


def test_render_banner_with_content():
    out = _render_inbox_banner({
        "unread_count": 2,
        "preview": ["projgamma@macbook → engram: check the X", "other → engram: ping"],
    })
    assert "📬 INBOX: 2 unread" in out
    assert "check the X" in out
    assert out.endswith("---\n\n")


# --- memory_send ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_send(respx_mock):
    respx_mock.post("/memory/send").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/abc-123"})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_send(
            to="engram",
            body="check the X",
            subject="heads up",
            project_dir="/Users/ixanadu/projects/projgamma",
        )
    assert "inbox/abc-123" in result
    assert "engram" in result
    assert "projgamma@macmini" in result


async def test_memory_send_empty_to():
    result = await memory_send(to="", body="x")
    assert "Error" in result


async def test_memory_send_empty_body():
    result = await memory_send(to="engram", body="")
    assert "Error" in result


# --- memory_inbox ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_inbox_empty(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(200, json={"status": "ok", "messages": []})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_inbox(project_dir="/Users/ixanadu/projects/engram")
    assert "empty" in result.lower()


@respx.mock(base_url="http://localhost:8920")
async def test_memory_inbox_with_messages(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    {
                        "id": "inbox/m1",
                        "to": "engram",
                        "from_": "projgamma@macbook",
                        "subject": "hi",
                        "body": "the body",
                        "thread_id": None,
                        "read_by": [],
                        "archived": False,
                        "created_at": "2026-04-14T00:00:00Z",
                    }
                ],
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_inbox(project_dir="/Users/ixanadu/projects/engram")
    assert "inbox/m1" in result
    assert "projgamma@macbook" in result
    assert "the body" in result


# --- memory_ack ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_ack(respx_mock):
    respx_mock.post("/memory/inbox/inbox/m1/ack").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/m1"})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_ack(
            message_id="inbox/m1",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "Acked inbox/m1" in result
    assert "engram@macmini" in result


# --- memory_reply ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_reply(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    {
                        "id": "inbox/m1",
                        "to": "engram",
                        "from_": "projgamma@macbook",
                        "subject": "original",
                        "body": "original body",
                        "thread_id": None,
                        "read_by": [],
                        "archived": False,
                        "created_at": "2026-04-14T00:00:00Z",
                    }
                ],
            },
        )
    )
    respx_mock.post("/memory/send").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/reply-1"})
    )
    respx_mock.post("/memory/inbox/inbox/m1/ack").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/m1"})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_reply(
            message_id="inbox/m1",
            body="got it, working on it",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "Replied to inbox/m1" in result
    assert "projgamma@macbook" in result


# --- memory_search banner injection ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_search_with_banner(respx_mock):
    respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "results": [
                    {
                        "namespace": "claude-code",
                        "key": "some_memory",
                        "value": "the value",
                        "scope": "project",
                        "user_id": "engram",
                        "tags": "",
                        "tags_search": "",
                        "score": 0.9,
                    }
                ],
                "inbox_banner": {
                    "unread_count": 1,
                    "preview": ["projgamma@macbook → engram: check the X"],
                },
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_search(
            query="anything",
            project_dir="/Users/ixanadu/projects/engram",
        )
    # Banner must come BEFORE results
    banner_idx = result.find("📬 INBOX")
    result_idx = result.find("some_memory")
    assert banner_idx != -1
    assert result_idx != -1
    assert banner_idx < result_idx
    assert "check the X" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_search_banner_with_no_results(respx_mock):
    respx_mock.post("/memory/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "results": [],
                "inbox_banner": {
                    "unread_count": 1,
                    "preview": ["projgamma@macbook → engram: ping"],
                },
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_search(
            query="anything",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "📬 INBOX" in result
    assert "No memories found." in result


# --- memory_inbox_archive ---

@respx.mock(base_url="http://localhost:8920")
async def test_memory_inbox_archive(respx_mock):
    respx_mock.post("/memory/inbox/inbox/m1/archive").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/m1"})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_inbox_archive(
            message_id="inbox/m1",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "Archived inbox/m1" in result
