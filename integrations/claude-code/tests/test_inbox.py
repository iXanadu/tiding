"""Tests for the MCP inbox tools."""

import httpx
import respx
from unittest.mock import patch

from engram_mcp.identity import compute_identity, is_admin_context, reader_to_address
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
    # Project sessions listen on the project, the machine, AND the fully-qualified
    # reader_identity — so fully-qualified replies still land.
    assert listen_set == ["engram", "machine:macmini", "engram@macmini"]


def test_compute_identity_admin():
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        reader, listen_set = compute_identity("/Users/ixanadu")
    assert reader == "machine:macmini"
    assert listen_set == ["machine:macmini"]


def test_reader_to_address_project():
    assert reader_to_address("engram@macmini") == "engram"
    assert reader_to_address("HomeBuyersCourse@laptop") == "HomeBuyersCourse"


def test_reader_to_address_machine():
    assert reader_to_address("machine:macmini") == "machine:macmini"


def test_reader_to_address_edge_cases():
    assert reader_to_address("") == ""
    assert reader_to_address("plain") == "plain"


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


@respx.mock(base_url="http://localhost:8920")
async def test_memory_send_forwards_guidance(respx_mock):
    """Server-supplied 'guidance' text must be appended to the tool return
    string. This is the whole point of the thin-wrapper architecture: the
    wrapper never needs to know what the guidance says."""
    respx_mock.post("/memory/send").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "id": "inbox/xyz",
                "guidance": "GUIDANCE_SENTINEL: addressing is flat",
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_send(
            to="engram",
            body="hello",
            project_dir="/Users/ixanadu/projects/projgamma",
        )
    assert "inbox/xyz" in result
    assert "GUIDANCE_SENTINEL" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_inbox_forwards_guidance(respx_mock):
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [],
                "guidance": "GUIDANCE_SENTINEL: call memory_inbox when banner appears",
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_inbox(project_dir="/Users/ixanadu/projects/engram")
    assert "empty" in result.lower()
    assert "GUIDANCE_SENTINEL" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_ack_forwards_guidance(respx_mock):
    respx_mock.post("/memory/inbox/inbox/m1/ack").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "id": "inbox/m1",
                "guidance": "GUIDANCE_SENTINEL: acks are per-reader",
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_ack(
            message_id="inbox/m1",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "Acked" in result
    assert "GUIDANCE_SENTINEL" in result


@respx.mock(base_url="http://localhost:8920")
async def test_memory_inbox_archive_forwards_guidance(respx_mock):
    respx_mock.post("/memory/inbox/inbox/m1/archive").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "id": "inbox/m1",
                "guidance": "GUIDANCE_SENTINEL: archive is global",
            },
        )
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        from engram_mcp.server import memory_inbox_archive
        result = await memory_inbox_archive(
            message_id="inbox/m1",
            project_dir="/Users/ixanadu/projects/engram",
        )
    assert "Archived" in result
    assert "GUIDANCE_SENTINEL" in result


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
async def test_memory_reply_addresses_project_not_reader_identity(respx_mock):
    """memory_reply must use the parent sender's loose address (project name),
    NOT their fully-qualified reader_identity. Regression: a bug sent replies
    to 'project@host' which no listen_set contained."""
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    {
                        "id": "inbox/m1",
                        "to": "engram",
                        "from_": "projgamma@macbook",  # reader_identity
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
    # Capture the send payload to assert the reply's 'to' field.
    send_route = respx_mock.post("/memory/send").mock(
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
    # Response message shows the resolved address, not the raw reader_identity
    assert "projgamma" in result
    # The critical assertion: the payload sent to /memory/send has to='projgamma'
    import json as _json
    sent_payload = _json.loads(send_route.calls.last.request.content)
    assert sent_payload["to"] == "projgamma", (
        f"Expected reply to address 'projgamma', got {sent_payload['to']!r}"
    )
    assert sent_payload["thread_id"] == "inbox/m1"


@respx.mock(base_url="http://localhost:8920")
async def test_memory_reply_admin_sender_passes_through(respx_mock):
    """When the parent was sent by an admin session (machine:host, no '@'),
    the reply should address machine:host as-is."""
    respx_mock.post("/memory/inbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "messages": [
                    {
                        "id": "inbox/m2",
                        "to": "engram",
                        "from_": "machine:macmini",
                        "subject": "admin note",
                        "body": "ping from admin",
                        "thread_id": None,
                        "read_by": [],
                        "archived": False,
                        "created_at": "2026-04-14T00:00:00Z",
                    }
                ],
            },
        )
    )
    send_route = respx_mock.post("/memory/send").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/reply-2"})
    )
    respx_mock.post("/memory/inbox/inbox/m2/ack").mock(
        return_value=httpx.Response(200, json={"status": "ok", "id": "inbox/m2"})
    )
    with patch.dict("os.environ", {"HOME": "/Users/ixanadu"}), \
         patch("engram_mcp.identity.hostname", return_value="macmini"):
        result = await memory_reply(
            message_id="inbox/m2",
            body="ack",
            project_dir="/Users/ixanadu/projects/engram",
        )
    import json as _json
    sent_payload = _json.loads(send_route.calls.last.request.content)
    assert sent_payload["to"] == "machine:macmini"


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
