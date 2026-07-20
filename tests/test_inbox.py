"""Integration tests for the inbox (inter-session messaging) endpoints."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from server.services.memory_service import inbox_autoresolve_stale
from server.services import principal_service as ps


@pytest_asyncio.fixture
async def enforced_client(services):
    """Client with require_auth=true, no legacy api_token — lets principal
    Bearer tokens authenticate (mirrors the fixture in test_permissions)."""
    with patch("server.auth.settings") as mock_settings, \
         patch("server.dependencies.settings") as mock_dep_settings:
        mock_settings.require_auth = True
        mock_settings.api_token = ""
        mock_dep_settings.require_auth = True
        mock_dep_settings.api_token = ""
        from server.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _cleanup_inbox(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace='fleet' AND scope='inbox'"
        )


@pytest.mark.asyncio
async def test_send_and_list_inbox(client, db_pool):
    await _cleanup_inbox(db_pool)

    resp = await client.post("/memory/send", json={
        "to": "engram",
        "subject": "check the X",
        "body": "the foo is broken, please look",
        "from_": "projgamma@macbook",
    })
    assert resp.status_code == 200, resp.text
    msg_id = resp.json()["id"]
    assert msg_id.startswith("inbox/")

    # List — reader is a different session, should see it
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram", "machine:macmini"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    msgs = data["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["id"] == msg_id
    assert m["to"] == "engram"
    assert m["from_"] == "projgamma@macbook"
    assert m["subject"] == "check the X"
    assert m["body"] == "the foo is broken, please look"
    assert m["read_by"] == []
    assert m["archived"] is False

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_ack_marks_read_per_reader(client, db_pool):
    await _cleanup_inbox(db_pool)

    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "note one",
        "from_": "projgamma@macbook",
    })
    msg_id = resp.json()["id"]

    # Reader A acks
    resp = await client.post(f"/memory/inbox/{msg_id}/ack", json={
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200

    # Reader A no longer sees it
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
    })
    assert resp.json()["messages"] == []

    # Reader B (different machine) still sees it
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macbook",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["read_by"] == ["engram@macmini"]

    # unread_only=False returns the message for reader A too
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": False,
    })
    assert len(resp.json()["messages"]) == 1

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_ack_idempotent(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "note",
    })
    msg_id = resp.json()["id"]
    for _ in range(3):
        r = await client.post(f"/memory/inbox/{msg_id}/ack", json={
            "reader_identity": "engram@macmini",
        })
        assert r.status_code == 200

    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": False,
    })
    msgs = resp.json()["messages"]
    assert msgs[0]["read_by"] == ["engram@macmini"]  # only once
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_ack_not_found(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/inbox/inbox/nonexistent/ack", json={
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_hides_from_all_readers(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "note",
    })
    msg_id = resp.json()["id"]

    resp = await client.post(f"/memory/inbox/{msg_id}/archive", json={
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200

    # Neither reader sees it now
    for reader in ("engram@macmini", "engram@macbook"):
        r = await client.post("/memory/inbox", json={
            "listen_set": ["engram"],
            "reader_identity": reader,
            "unread_only": False,
        })
        assert r.json()["messages"] == []
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_machine_addressing(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "machine:macmini",
        "body": "restart ollama please",
        "from_": "ixanadu@macbook",
    })
    assert resp.status_code == 200
    msg_id = resp.json()["id"]

    # Admin Claude on macmini listens only on machine:macmini
    r = await client.post("/memory/inbox", json={
        "listen_set": ["machine:macmini"],
        "reader_identity": "machine:macmini",
        "unread_only": True,
    })
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["to"] == "machine:macmini"

    # Project Claude on macbook does NOT see it (wrong machine)
    r = await client.post("/memory/inbox", json={
        "listen_set": ["engram", "machine:macbook"],
        "reader_identity": "engram@macbook",
        "unread_only": True,
    })
    assert r.json()["messages"] == []
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_banner_on_search(client, db_pool):
    await _cleanup_inbox(db_pool)
    # Seed 2 inbox messages
    await client.post("/memory/send", json={
        "to": "engram",
        "subject": "first",
        "body": "body one",
        "from_": "projgamma@macbook",
    })
    await client.post("/memory/send", json={
        "to": "engram",
        "subject": "second",
        "body": "body two",
        "from_": "projgamma@macbook",
    })

    # Regular search with listen_set should get a banner
    resp = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "anything",
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["inbox_banner"] is not None
    assert data["inbox_banner"]["unread_count"] == 2
    assert len(data["inbox_banner"]["preview"]) == 2

    # Without listen_set, no banner
    resp = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "anything",
    })
    assert resp.json().get("inbox_banner") is None

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_banner_on_set(client, db_pool):
    """memory_set returns an inbox banner when the writing session has unread
    mail — lets write-heavy sessions pick up messages without polling."""
    await _cleanup_inbox(db_pool)
    await client.post("/memory/send", json={
        "to": "engram",
        "subject": "ping",
        "body": "you have mail",
        "from_": "admin@macmini",
    })

    resp = await client.post("/memory/set", json={
        "namespace": "fleet",
        "key": "banner-on-set-probe",
        "value": "hello",
        "scope": "machine",
        "user_id": "macmini",
        "listen_set": ["engram", "machine:macmini", "engram@macmini"],
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["inbox_banner"] is not None
    assert data["inbox_banner"]["unread_count"] == 1
    preview = " ".join(data["inbox_banner"]["preview"])
    assert "admin@macmini" in preview
    assert "ping" in preview

    # Without listen_set, no banner on set (matches /search behavior)
    resp = await client.post("/memory/set", json={
        "namespace": "fleet",
        "key": "banner-on-set-probe-2",
        "value": "hello",
        "scope": "machine",
        "user_id": "macmini",
    })
    assert resp.json().get("inbox_banner") is None

    # Cleanup the probe rows
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE key LIKE 'banner-on-set-probe%'"
        )
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_memory_set_records_project_and_cwd_metadata(client, db_pool):
    """X-Engram-Project and X-Engram-Cwd headers land in memory metadata so
    the dashboard can filter by folder of origin."""
    resp = await client.post(
        "/memory/set",
        json={
            "namespace": "fleet",
            "key": "provenance-probe",
            "value": "hello",
            "scope": "machine",
            "user_id": "macmini",
        },
        headers={
            "X-Engram-Project": "engram",
            "X-Engram-Cwd": "/Users/ixanadu/projects/engram",
            "X-Engram-Machine": "macmini",
        },
    )
    assert resp.status_code == 200, resp.text

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM memories WHERE key=$1 AND namespace='fleet'",
            "provenance-probe",
        )
    assert row is not None
    import json as _json
    md = row["metadata"]
    if isinstance(md, str):
        md = _json.loads(md)
    assert md.get("project") == "engram"
    assert md.get("cwd") == "/Users/ixanadu/projects/engram"
    assert md.get("machine") == "macmini"

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE key='provenance-probe'")


@pytest.mark.asyncio
async def test_banner_absent_when_empty(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "anything",
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200
    assert resp.json().get("inbox_banner") is None


@pytest.mark.asyncio
async def test_search_excludes_inbox_scope(client, db_pool):
    """Inbox messages must NEVER surface in a regular vector search."""
    await _cleanup_inbox(db_pool)
    # Put an inbox message whose body is very searchable
    await client.post("/memory/send", json={
        "to": "engram",
        "subject": "pineapple pizza",
        "body": "the ultimate test phrase about pineapples",
    })
    # Seed a normal memory for the same query so we know search works
    await client.post("/memory/set", json={
        "namespace": "test-inbox-xclusion",
        "key": "fruit_pref",
        "value": "I enjoy pineapples on pizza",
        "scope": "user",
        "user_id": "default",
    })
    resp = await client.post("/memory/search", json={
        "namespace": "test-inbox-xclusion",
        "query": "pineapple",
        "limit": 10,
    })
    assert resp.status_code == 200
    for r in resp.json()["results"]:
        assert not r["key"].startswith("inbox/")
    # And searching fleet for the same phrase must not surface inbox
    resp = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "pineapple",
        "limit": 10,
    })
    for r in resp.json()["results"]:
        assert not r["key"].startswith("inbox/")

    await client.post("/memory/forget", json={
        "namespace": "test-inbox-xclusion",
        "key": "fruit_pref",
        "scope": "user",
        "user_id": "default",
    })
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_case_insensitive_addressing(client, db_pool):
    """Sending to 'ProjGamma' must deliver to a listener on 'projgamma'."""
    await _cleanup_inbox(db_pool)

    resp = await client.post("/memory/send", json={
        "to": "ProjGamma",
        "body": "case test",
        "from_": "admin@macmini",
    })
    assert resp.status_code == 200
    assert resp.json()["id"].startswith("inbox/")

    resp = await client.post("/memory/inbox", json={
        "listen_set": ["projgamma", "machine:macmini"],
        "reader_identity": "projgamma@macmini",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["to"] == "projgamma"
    assert msgs[0]["body"] == "case test"

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_address_validation(client, db_pool):
    await _cleanup_inbox(db_pool)
    # Empty
    resp = await client.post("/memory/send", json={"to": "", "body": "x"})
    assert resp.status_code == 422
    # Bad chars
    resp = await client.post("/memory/send", json={"to": "bad address!", "body": "x"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_thread_id_preserved(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "root",
        "thread_id": "t-123",
    })
    mid = resp.json()["id"]
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    assert msgs[0]["thread_id"] == "t-123"
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_guidance_present_on_inbox_responses(client, db_pool):
    """Each inbox endpoint must return a non-empty guidance string so the
    MCP bridge can surface usage hints without requiring a Claude restart
    to update docstrings."""
    await _cleanup_inbox(db_pool)

    # send
    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "hello",
        "from_": "projgamma@macbook",
    })
    assert resp.status_code == 200
    send_data = resp.json()
    assert send_data.get("guidance"), "send response must include guidance"
    assert "addressing" in send_data["guidance"].lower()
    msg_id = send_data["id"]

    # list with messages
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
    })
    list_data = resp.json()
    assert list_data.get("guidance"), "list (non-empty) must include guidance"
    assert "memory_reply" in list_data["guidance"]

    # ack
    resp = await client.post(
        f"/memory/inbox/{msg_id}/ack",
        json={"reader_identity": "engram@macmini"},
    )
    ack_data = resp.json()
    assert ack_data.get("guidance"), "ack must include guidance"
    assert "per-reader" in ack_data["guidance"].lower()

    # list empty (different guidance path)
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["nonexistent-project-xyz"],
        "reader_identity": "nonexistent@macmini",
        "unread_only": True,
    })
    empty_data = resp.json()
    assert empty_data.get("guidance"), "empty list must include guidance"
    assert "banner" in empty_data["guidance"].lower()

    # archive
    # send a fresh message to archive (ack'd one still exists)
    resp = await client.post("/memory/send", json={
        "to": "engram",
        "body": "archive me",
        "from_": "projgamma@macbook",
    })
    arch_id = resp.json()["id"]
    resp = await client.post(
        f"/memory/inbox/{arch_id}/archive",
        json={"reader_identity": "engram@macmini"},
    )
    arch_data = resp.json()
    assert arch_data.get("guidance"), "archive must include guidance"
    assert "archive" in arch_data["guidance"].lower()

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_inbox_strips_toolcall_trailer(client, db_pool):
    """Defensive strip for model composition leak: memory_reply bodies sometimes
    trail </body></invoke> tag fragments when the parameter value bleeds into
    the tool-call XML. Server must strip before persisting."""
    await _cleanup_inbox(db_pool)

    cases = [
        ("clean body</body></invoke>", "clean body"),
        ("trailing </invoke>\n", "trailing"),
        ("double </body></body></invoke>  ", "double"),
        ("with </parameter></invoke>", "with"),
        ("mixed </body> </invoke>", "mixed"),
        ("no bleed here", "no bleed here"),
    ]
    for raw_body, want in cases:
        resp = await client.post("/memory/send", json={
            "to": "engram",
            "subject": f"subj </body></invoke>",
            "body": raw_body,
            "from_": "engram@macmini",
        })
        assert resp.status_code == 200, resp.text

    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    stored_bodies = {m["body"] for m in msgs}
    stored_subjects = {m["subject"] for m in msgs}
    for _, want in cases:
        assert want in stored_bodies, f"expected clean body {want!r}, got {stored_bodies}"
    # Subject should also be stripped
    assert stored_subjects == {"subj"}
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_banner_caps_at_preview_limit(client, db_pool):
    await _cleanup_inbox(db_pool)
    for i in range(7):
        await client.post("/memory/send", json={
            "to": "engram",
            "subject": f"msg {i}",
            "body": f"body {i}",
            "from_": "projgamma@macbook",
        })
    resp = await client.post("/memory/search", json={
        "namespace": "fleet",
        "query": "anything",
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
    })
    banner = resp.json()["inbox_banner"]
    # Preview capped at 5; unread_count reflects the capped fetch
    assert len(banner["preview"]) == 5
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_autocorrect_admin_colon_host(client, db_pool):
    """admin:host → machine:host — delivered and flagged."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "admin:hostb",
        "body": "please install pgvector",
        "from_": "projbeta@laptop",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["corrected_from"] == "admin:hostb"
    assert "AUTO-CORRECTED" in data["guidance"]

    # Message should land for machine:hostb listener
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["admin", "machine:hostb", "admin@hostb"],
        "reader_identity": "admin@hostb",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "please install pgvector"
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_autocorrect_host_colon_project(client, db_pool):
    """host:project → project — delivered as broadcast."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "laptop:projbeta",
        "body": "schema conflicts need resolution",
        "from_": "projalpha@laptop",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["corrected_from"] == "laptop:projbeta"
    assert "AUTO-CORRECTED" in data["guidance"]

    # Message should land for projbeta listener
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["projbeta", "machine:laptop", "projbeta@laptop"],
        "reader_identity": "projbeta@laptop",
        "unread_only": True,
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "schema conflicts need resolution"
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_resolve_drains_from_default_view(client, db_pool):
    """A resolved message is hidden from the default (open-only) inbox view but
    retrievable with include_resolved=True, and records who resolved it."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram", "body": "please look", "from_": "admin@macmini",
    })
    msg_id = resp.json()["id"]

    # Visible while open
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    assert len(resp.json()["messages"]) == 1
    assert resp.json()["messages"][0]["status"] == "open"

    # Resolve it
    resp = await client.post(f"/memory/inbox/{msg_id}/resolve", json={
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 200
    assert "resolved" in resp.json()["guidance"].lower()

    # Gone from default view for a DIFFERENT, never-acked reader (fresh-reader fix)
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    assert resp.json()["messages"] == []

    # Retrievable with include_resolved
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
        "unread_only": False, "include_resolved": True,
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["status"] == "resolved"
    assert msgs[0]["resolved_by"] == "engram@macmini"
    assert msgs[0]["resolved_at"] is not None
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_resolve_not_found(client, db_pool):
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/inbox/inbox/nope/resolve", json={
        "reader_identity": "engram@macmini",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_supersede_marks_prior_message(client, db_pool):
    """Sending with supersedes=<id> marks the prior message superseded so it
    drops out of the default view (the laptop saga: latest wins)."""
    await _cleanup_inbox(db_pool)
    r1 = await client.post("/memory/send", json={
        "to": "engram", "subject": "v1", "body": "laptop is the hub",
        "from_": "admin@macmini",
    })
    old_id = r1.json()["id"]

    r2 = await client.post("/memory/send", json={
        "to": "engram", "subject": "v2", "body": "actually laptop is sidelined",
        "from_": "admin@macmini", "supersedes": old_id,
    })
    new_id = r2.json()["id"]

    # Default view: only the new message
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    msgs = resp.json()["messages"]
    assert [m["id"] for m in msgs] == [new_id]
    assert msgs[0]["supersedes"] == old_id

    # History shows the old one as superseded, pointing at the replacement
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
        "unread_only": False, "include_resolved": True,
    })
    by_id = {m["id"]: m for m in resp.json()["messages"]}
    assert by_id[old_id]["status"] == "superseded"
    assert by_id[old_id]["superseded_by"] == new_id
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_resolved_not_counted_in_banner(client, db_pool):
    """A resolved message must not raise the 📬 banner — drained mail can't wake
    a session."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram", "body": "handle me", "from_": "admin@macmini",
    })
    msg_id = resp.json()["id"]
    await client.post(f"/memory/inbox/{msg_id}/resolve", json={
        "reader_identity": "engram@macmini",
    })
    resp = await client.post("/memory/search", json={
        "namespace": "fleet", "query": "anything",
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    assert resp.json().get("inbox_banner") is None
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_staleness_annotation(client, db_pool):
    """An open message older than the staleness threshold is flagged is_stale
    with an age, and the list guidance warns to verify before acting. Never
    deleted — just annotated."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram", "body": "old coordination", "from_": "admin@macmini",
    })
    msg_id = resp.json()["id"]
    # Backdate created_at past the 72h threshold
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET created_at = now() - interval '100 hours' "
            "WHERE key=$1 AND scope='inbox'",
            msg_id,
        )
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    data = resp.json()
    m = data["messages"][0]
    assert m["is_stale"] is True
    assert m["age_hours"] >= 72
    assert m["status"] == "open"  # still actionable, just flagged
    assert "STALE" in data["guidance"]
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_digest_in_list_guidance(client, db_pool):
    """List guidance carries a status digest (open / hidden counts)."""
    await _cleanup_inbox(db_pool)
    keep = await client.post("/memory/send", json={
        "to": "engram", "body": "open one", "from_": "admin@macmini",
    })
    done = await client.post("/memory/send", json={
        "to": "engram", "body": "done one", "from_": "admin@macmini",
    })
    await client.post(f"/memory/inbox/{done.json()['id']}/resolve", json={
        "reader_identity": "engram@macmini",
    })
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    g = resp.json()["guidance"]
    assert "1 open" in g
    assert "resolved/superseded hidden" in g
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_backcompat_missing_status_is_open(client, db_pool):
    """Pre-lifecycle messages (no status in metadata) must still show as open."""
    await _cleanup_inbox(db_pool)
    resp = await client.post("/memory/send", json={
        "to": "engram", "body": "legacy", "from_": "admin@macmini",
    })
    msg_id = resp.json()["id"]
    # Simulate an old message: strip the status key entirely
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET metadata = metadata - 'status' "
            "WHERE key=$1 AND scope='inbox'",
            msg_id,
        )
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["engram"], "reader_identity": "engram@macbook",
    })
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["status"] == "open"
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_no_autocorrect_for_valid_addresses(client, db_pool):
    """Reserved prefixes and bare names pass through without correction."""
    await _cleanup_inbox(db_pool)
    for addr in ["projbeta", "machine:hostb", "admin@hostb"]:
        resp = await client.post("/memory/send", json={
            "to": addr,
            "body": "test",
            "from_": "test@test",
        })
        assert resp.status_code == 200
        assert resp.json()["corrected_from"] is None
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_inbox_limit_keeps_newest_in_window(client, db_pool):
    """A `limit` smaller than the backlog must return the NEWEST N messages —
    for BOTH display (oldest-first reading order) and the watcher (newest-first).
    Regression for IB-5: the display path selected the OLDEST N
    (`ORDER BY created_at ASC LIMIT`), hiding the most-recent mail once
    unread > limit. Fix: inner-select the newest N (`DESC LIMIT`), then present
    in the caller's reading order. The OLD end is truncated, never the new end.
    """
    await _cleanup_inbox(db_pool)
    # Send a backlog larger than the query limit, in order. ids[-1] is newest.
    ids = []
    for i in range(4):
        resp = await client.post("/memory/send", json={
            "to": "engram", "body": f"m{i}", "from_": "peer@elsewhere",
        })
        assert resp.status_code == 200
        ids.append(resp.json()["id"])
    newest_id = ids[-1]
    oldest_id = ids[0]

    common = {
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
        "unread_only": True,
        "limit": 2,  # smaller than the 4-message backlog
    }

    # Display (oldest-first reading order): window is the NEWEST 2, shown
    # oldest-first. The newest is present (last); the oldest is truncated.
    resp = await client.post("/memory/inbox", json={**common, "newest_first": False})
    window = [m["id"] for m in resp.json()["messages"]]
    assert len(window) == 2
    assert window == [ids[2], ids[3]]      # newest 2, oldest-first reading order
    assert newest_id == window[-1]
    assert oldest_id not in window         # the OLD end is truncated, not the new

    # Watcher (newest-first): same newest 2, newest-first order.
    resp = await client.post("/memory/inbox", json={**common, "newest_first": True})
    window = [m["id"] for m in resp.json()["messages"]]
    assert len(window) == 2
    assert window == [ids[3], ids[2]]      # newest 2, newest-first
    assert newest_id == window[0]

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_autoresolve_drains_read_and_stale(client, db_pool):
    """The stale-sweep resolves READ + stale mail, but never unread or fresh
    mail — draining the read-but-unresolved pile without hiding undelivered
    messages, and reversibly (resolve, not delete)."""
    await _cleanup_inbox(db_pool)

    async def _send(body):
        r = await client.post("/memory/send", json={
            "to": "engram", "body": body, "from_": "peer@elsewhere",
        })
        assert r.status_code == 200
        return r.json()["id"]

    read_stale = await _send("read + stale")
    unread_stale = await _send("unread + stale")
    read_fresh = await _send("read + fresh")

    # mark the two "read" ones read
    for mid in (read_stale, read_fresh):
        r = await client.post(f"/memory/inbox/{mid}/ack",
                              json={"reader_identity": "engram@macmini"})
        assert r.status_code == 200

    # backdate the two "stale" ones past the 72h threshold
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET created_at = now() - interval '100 hours' "
            "WHERE scope='inbox' AND key = ANY($1::text[])",
            [read_stale, unread_stale],
        )

    resolved = await inbox_autoresolve_stale(older_than_hours=72)
    assert resolved == 1  # only read_stale qualifies (read AND stale)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, metadata->>'status' AS status, "
            "metadata->>'resolved_by' AS resolved_by "
            "FROM memories WHERE scope='inbox' AND key = ANY($1::text[])",
            [read_stale, unread_stale, read_fresh],
        )
    state = {r["key"]: (r["status"], r["resolved_by"]) for r in rows}
    assert state[read_stale] == ("resolved", "system:stale-sweep")
    assert state[unread_stale][0] in (None, "open")   # unread → never touched
    assert state[read_fresh][0] in (None, "open")     # fresh → never touched

    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_inbox_authority_is_server_derived_not_spoofable(enforced_client, db_pool):
    """MSG-1/MSG-2: `from_principal` and `authority` are stamped by the SERVER
    from the authenticated token, never from client input. A worker cannot forge
    the owner badge even by self-labeling `from_="rob"`; only the owner's own
    (is_admin) token stamps authority=true.
    """
    await _cleanup_inbox(db_pool)
    # type="human" does not auto-generate a token — pass one explicitly.
    _, owner_tok = await ps.create_principal(
        name="ib-authtest-owner", type="human", is_admin=True,
        token="owner-testtok-fixture",
        write_namespaces=["fleet"], read_namespaces=["fleet"],
    )
    _, worker_tok = await ps.create_principal(
        name="ib-authtest-worker", type="agent", is_admin=False,
        token="worker-testtok-fixture",
        write_namespaces=["fleet"], read_namespaces=["fleet"],
    )
    try:
        # Owner: self-labels from_ freely; the server stamps a verified owner.
        r = await enforced_client.post("/memory/send", json={
            "to": "authprobe", "body": "owner directive", "from_": "rob",
        }, headers={"Authorization": f"Bearer {owner_tok}"})
        assert r.status_code == 200, r.text

        # Worker forgery: claims from_="rob" AND tries to set authority in the
        # body (not a real request field) — the server must expose the true
        # sender and refuse the owner badge.
        r = await enforced_client.post("/memory/send", json={
            "to": "authprobe", "body": "forgery", "from_": "rob",
            "authority": True,  # inert: not a settable field
        }, headers={"Authorization": f"Bearer {worker_tok}"})
        assert r.status_code == 200, r.text

        r = await enforced_client.post("/memory/inbox", json={
            "listen_set": ["authprobe"], "unread_only": False, "limit": 20,
        }, headers={"Authorization": f"Bearer {owner_tok}"})
        assert r.status_code == 200
        by_body = {m["body"]: m for m in r.json()["messages"]}

        owner_msg = by_body["owner directive"]
        assert owner_msg["authority"] is True
        assert owner_msg["from_principal"] == "ib-authtest-owner"
        assert owner_msg["from_"] == "rob"      # self-asserted label preserved

        forge = by_body["forgery"]
        assert forge["authority"] is False                      # forgery refused
        assert forge["from_principal"] == "ib-authtest-worker"  # true sender exposed
        assert forge["from_"] == "rob"          # they may CLAIM a label, but the
        # verified badge and true principal give them away.
    finally:
        await _cleanup_inbox(db_pool)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM principals WHERE name = ANY($1::text[])",
                ["ib-authtest-owner", "ib-authtest-worker"],
            )


@pytest.mark.asyncio
async def test_inbox_intent_stored_and_validated(client, db_pool):
    """MSG-3: `intent` is a client field — stored and returned as-is; an unknown
    intent is rejected (422); omitting it yields None (back-compat waking default).
    """
    await _cleanup_inbox(db_pool)
    # valid intent → stored
    r = await client.post("/memory/send", json={
        "to": "intentprobe", "body": "proceed now", "intent": "proceed",
    })
    assert r.status_code == 200, r.text
    # omitted intent → None (legacy/back-compat)
    r = await client.post("/memory/send", json={
        "to": "intentprobe", "body": "no intent",
    })
    assert r.status_code == 200, r.text
    # unknown intent → 422, never stored
    r = await client.post("/memory/send", json={
        "to": "intentprobe", "body": "bad", "intent": "shout",
    })
    assert r.status_code == 422, r.text

    r = await client.post("/memory/inbox", json={
        "listen_set": ["intentprobe"], "unread_only": False, "limit": 20,
    })
    assert r.status_code == 200
    by_body = {m["body"]: m for m in r.json()["messages"]}
    assert by_body["proceed now"]["intent"] == "proceed"
    assert by_body["no intent"]["intent"] is None
    assert "bad" not in by_body  # rejected before storage
    await _cleanup_inbox(db_pool)


# --- Presence / liveness roster (MSG-4) ----------------------------------

async def _cleanup_presence(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace='fleet' AND scope='presence'"
        )


@pytest.mark.asyncio
async def test_presence_heartbeat_and_roster(client, db_pool):
    """MSG-4: heartbeats upsert one row per identity; the roster answers
    'who is on this project, in what state' with staleness annotation."""
    await _cleanup_presence(db_pool)

    # Two agents on one project, one on another
    for identity, project, provider, state in [
        ("foo", "foo", "claude", "running"),
        ("foo-grok", "foo", "grok", "awaiting-input"),
        ("bar", "bar", "claude", "running"),
    ]:
        r = await client.post("/memory/presence", json={
            "identity": identity, "project": project,
            "provider": provider, "state": state,
            "channels": ["#courseware"] if project == "foo" else [],
        })
        assert r.status_code == 200, r.text

    # Project roster: only foo's two agents
    r = await client.post("/memory/roster", json={"project": "foo"})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert {e["identity"] for e in entries} == {"foo", "foo-grok"}
    grok = next(e for e in entries if e["identity"] == "foo-grok")
    assert grok["provider"] == "grok"
    assert grok["state"] == "awaiting-input"
    assert grok["is_stale"] is False
    assert grok["age_seconds"] < 60

    # Channel roster crosses projects (both foo agents joined #courseware)
    r = await client.post("/memory/roster", json={"channel": "#courseware"})
    assert {e["identity"] for e in r.json()["entries"]} == {"foo", "foo-grok"}

    # Whole-box roster sees all three
    r = await client.post("/memory/roster", json={})
    assert {e["identity"] for e in r.json()["entries"]} == {"foo", "foo-grok", "bar"}

    # Heartbeat is an UPSERT: state transition, still one row
    r = await client.post("/memory/presence", json={
        "identity": "foo-grok", "project": "foo", "provider": "grok",
        "state": "done",
    })
    assert r.status_code == 200
    # done is hidden by default...
    r = await client.post("/memory/roster", json={"project": "foo"})
    assert {e["identity"] for e in r.json()["entries"]} == {"foo"}
    # ...but visible with include_done
    r = await client.post("/memory/roster", json={"project": "foo", "include_done": True})
    assert {e["identity"] for e in r.json()["entries"]} == {"foo", "foo-grok"}

    # Invalid state → 422
    r = await client.post("/memory/presence", json={
        "identity": "x", "project": "x", "state": "zombie",
    })
    assert r.status_code == 422

    # Staleness: backdate foo's heartbeat past the threshold
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET last_used_at = NOW() - INTERVAL '11 minutes' "
            "WHERE scope='presence' AND key='presence/foo'"
        )
    r = await client.post("/memory/roster", json={"project": "foo"})
    foo = next(e for e in r.json()["entries"] if e["identity"] == "foo")
    assert foo["is_stale"] is True

    await _cleanup_presence(db_pool)


# --- Broadcast: #channels + multi-recipient fan-out (MSG-5) ---------------

@pytest.mark.asyncio
async def test_channel_send_and_subscribe(client, db_pool):
    """MSG-5b: '#channel' is a valid address; agents from DIFFERENT projects
    that include the channel in their listen_set all receive one message."""
    await _cleanup_inbox(db_pool)
    r = await client.post("/memory/send", json={
        "to": "#courseware", "body": "coalition broadcast", "from_": "rob",
        "intent": "authority-directive",
    })
    assert r.status_code == 200, r.text

    # Agents from three different projects, all subscribed to the channel
    for reader, home in [("projalpha@m", "projalpha"),
                         ("projgamma@m", "projgamma"),
                         ("projbeta@m", "projbeta")]:
        resp = await client.post("/memory/inbox", json={
            "listen_set": [home, "#courseware"],
            "reader_identity": reader,
            "unread_only": True,
        })
        msgs = resp.json()["messages"]
        assert len(msgs) == 1, f"{reader} missed the channel broadcast"
        assert msgs[0]["to"] == "#courseware"
        assert msgs[0]["intent"] == "authority-directive"

    # An agent NOT subscribed does not receive it
    resp = await client.post("/memory/inbox", json={
        "listen_set": ["unrelated"],
        "reader_identity": "unrelated@m",
        "unread_only": True,
    })
    assert resp.json()["messages"] == []
    await _cleanup_inbox(db_pool)


@pytest.mark.asyncio
async def test_multi_recipient_fanout(client, db_pool):
    """MSG-5c: 'to' accepts a list — each recipient gets their own message id."""
    await _cleanup_inbox(db_pool)
    r = await client.post("/memory/send", json={
        "to": ["alpha", "beta", "alpha"],   # dupe deduped
        "body": "ad-hoc fanout",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ids"] is not None and len(data["ids"]) == 2
    assert data["id"] == data["ids"][0]

    for who in ("alpha", "beta"):
        resp = await client.post("/memory/inbox", json={
            "listen_set": [who], "reader_identity": f"{who}@m",
            "unread_only": True,
        })
        msgs = resp.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["body"] == "ad-hoc fanout"

    # Single-string 'to' still returns ids=None (back-compat shape)
    r = await client.post("/memory/send", json={"to": "alpha", "body": "solo"})
    assert r.json()["ids"] is None
    await _cleanup_inbox(db_pool)


# --- Long-poll wait: the any-harness wake primitive ------------------------

@pytest.mark.asyncio
async def test_inbox_wait_returns_new_mail_and_filters(client, db_pool):
    """/memory/inbox/wait returns mail newer than `since`, excludes fyi and
    self-echo, and times out cleanly when nothing arrives."""
    await _cleanup_inbox(db_pool)
    from datetime import datetime, timezone, timedelta
    cursor = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()

    # seed: one waking message, one fyi, one self-echo — all newer than cursor
    for body, from_, intent in [
        ("wake me", "peer@elsewhere", "action"),
        ("just fyi", "peer@elsewhere", "fyi"),
        ("own echo", "waittest@m", None),
    ]:
        payload = {"to": "waittest", "body": body, "from_": from_}
        if intent:
            payload["intent"] = intent
        r = await client.post("/memory/send", json=payload)
        assert r.status_code == 200

    r = await client.post("/memory/inbox/wait", json={
        "listen_set": ["waittest"], "reader_identity": "waittest@m",
        "timeout_seconds": 5, "since": cursor,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert [m["body"] for m in data["messages"]] == ["wake me"]  # fyi + self filtered

    # timeout path: cursor in the future → nothing qualifies
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await client.post("/memory/inbox/wait", json={
        "listen_set": ["waittest"], "timeout_seconds": 0, "since": future,
    })
    assert r.json()["status"] == "timeout"
    assert r.json()["messages"] == []

    # bounds: absurd timeout rejected
    r = await client.post("/memory/inbox/wait", json={
        "listen_set": ["waittest"], "timeout_seconds": 9999,
    })
    assert r.status_code == 422
    await _cleanup_inbox(db_pool)
