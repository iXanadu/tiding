"""Integration tests for the inbox (inter-session messaging) endpoints."""

import pytest


async def _cleanup_inbox(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace='claude-code' AND scope='inbox'"
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
        "namespace": "claude-code",
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
        "namespace": "claude-code",
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
        "namespace": "claude-code",
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
        "namespace": "claude-code",
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
            "namespace": "claude-code",
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
            "SELECT metadata FROM memories WHERE key=$1 AND namespace='claude-code'",
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
        "namespace": "claude-code",
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
    # And searching claude-code for the same phrase must not surface inbox
    resp = await client.post("/memory/search", json={
        "namespace": "claude-code",
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
        "namespace": "claude-code",
        "query": "anything",
        "listen_set": ["engram"],
        "reader_identity": "engram@macmini",
    })
    banner = resp.json()["inbox_banner"]
    # Preview capped at 5; unread_count reflects the capped fetch
    assert len(banner["preview"]) == 5
    await _cleanup_inbox(db_pool)
