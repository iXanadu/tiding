"""ADDR-2 store half: ASK-class mail nobody has ever read.

Measured on live data 2026-08-20, and the measurement chose the predicate:
93% of action-class mail is still `open` because resolution barely happens, so
`open` fires on almost everything and gets tuned out. NEVER-READ is ~8.5% and
every row is a genuine "this reached nobody" — 221 such asks to agent
addresses in one week, the oldest sitting 361 hours.

The owner's framing, which this serves: you send a non-info message expecting
it to land, you will not poll for an ack, and you will sit idle for hours. The
store cannot fix that alone — it can only say what was never read. Whether
anyone is home is the spawner's verdict.
"""

import uuid

import pytest

from server.services.memory_service import unanswered_asks


async def _send(client, to, subject, intent):
    r = await client.post("/memory/send", json={
        "to": to, "subject": subject, "body": "please act", "intent": intent,
    })
    assert r.status_code == 200
    return r.json()["id"]


@pytest.mark.asyncio
async def test_an_unread_ask_surfaces_and_an_fyi_does_not(client, db_pool):
    addr = f"addr2-nobody-{uuid.uuid4().hex[:8]}"
    ask = await _send(client, addr, "ADDR2 ask", "action")
    await _send(client, addr, "ADDR2 chatter", "fyi")

    rows = await unanswered_asks(min_age_hours=0, limit=200)
    ids = {r["id"] for r in rows}

    assert ask in ids, "an action-class ask nobody read did not surface"
    subjects = {r["subject"] for r in rows}
    assert "ADDR2 chatter" not in subjects, (
        "an fyi surfaced — informational mail nobody reads is not a problem, "
        "and including it is how the signal gets tuned out"
    )


@pytest.mark.asyncio
async def test_reading_it_removes_it_even_when_still_open(client, db_pool):
    """The predicate is NEVER-READ, not unresolved. Resolution culture has
    collapsed (93% open on live data), so keying on `open` would fire on
    nearly everything."""
    addr = f"addr2-reader-{uuid.uuid4().hex[:8]}"
    ask = await _send(client, addr, "ADDR2 will-be-read", "action")

    assert ask in {r["id"] for r in await unanswered_asks(min_age_hours=0, limit=200)}

    r = await client.post(f"/memory/inbox/{ask}/ack",
                          json={"reader_identity": f"{addr}@macmini"})
    assert r.status_code == 200

    rows = await unanswered_asks(min_age_hours=0, limit=200)
    assert ask not in {x["id"] for x in rows}, (
        "a read ask still surfaced — someone HAS seen it, so it is no longer "
        "the 'reached nobody' case this exists to catch"
    )


@pytest.mark.asyncio
async def test_age_floor_is_honoured(client, db_pool):
    addr = f"addr2-fresh-{uuid.uuid4().hex[:8]}"
    ask = await _send(client, addr, "ADDR2 brand new", "action")

    fresh = await unanswered_asks(min_age_hours=1, limit=200)
    assert ask not in {r["id"] for r in fresh}, (
        "a seconds-old ask was reported as unanswered — nagging about mail "
        "nobody has had a chance to read is how an alarm gets ignored"
    )


@pytest.mark.asyncio
async def test_channel_addressed_asks_are_included(client, db_pool):
    """The load-bearing case. Channels are the mode we tell everyone to
    prefer, and they are exempt from BOTH the sweep (root is skipped) and the
    climb (a root has no ancestor). So the safest address to send to has the
    weakest follow-up in the system — which is exactly why the owner's ask sat
    27 hours. If this query excluded channels it would miss the whole point."""
    proj = f"addr2proj{uuid.uuid4().hex[:6]}"
    ask = await _send(client, proj, "ADDR2 channel ask", "action")

    rows = await unanswered_asks(min_age_hours=0, limit=200)
    assert ask in {r["id"] for r in rows}
    row = next(r for r in rows if r["id"] == ask)
    assert row["address"] == proj
    assert row["age_hours"] >= 0


@pytest.mark.asyncio
async def test_endpoint_returns_facts_not_verdicts(client, db_pool):
    addr = f"addr2-api-{uuid.uuid4().hex[:8]}"
    await _send(client, addr, "ADDR2 via api", "action")

    r = await client.get("/admin/inbox/unanswered?min_age_hours=0&limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["count"] >= 1
    row = body["unanswered"][0]
    for field in ("id", "address", "subject", "intent", "age_hours"):
        assert field in row
    # No liveness claim may appear here — that verdict is the spawner's.
    assert "alive" not in row and "dead" not in row


# --- THE CORRECTION THAT COST A RETRACTED FINDING -------------------------
# First version keyed on read_by=[] alone and reported 197 "unread" asks at
# agent addresses. The owner rejected it from lived experience, and the
# measurement agreed with him: of 159 never-acked asks at one grok address,
# 159 had a REPLY IN THREAD. Agents answer mail without ever calling ack.
# read_by measures ACK DISCIPLINE, not delivery. A reply proves it landed.

@pytest.mark.asyncio
async def test_a_replied_to_ask_is_not_unanswered_even_if_never_acked(client, db_pool):
    addr = f"addr2-replied-{uuid.uuid4().hex[:8]}"
    r = await client.post("/memory/send", json={
        "to": addr, "subject": "ADDR2 answered ask", "body": "please act",
        "intent": "action", "thread_id": f"t-{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 200
    ask = r.json()["id"]
    thread = (await client.post("/memory/inbox", json={
        "listen_set": [addr], "reader_identity": f"{addr}@macmini",
        "unread_only": False, "limit": 5,
    })).json()["messages"][0]["thread_id"]

    assert ask in {x["id"] for x in await unanswered_asks(min_age_hours=0, limit=300)}

    # somebody answers in-thread, WITHOUT ever acking the original
    r2 = await client.post("/memory/send", json={
        "to": "someone-else", "subject": "re: ADDR2", "body": "on it",
        "intent": "fyi", "thread_id": thread,
    })
    assert r2.status_code == 200

    rows = await unanswered_asks(min_age_hours=0, limit=300)
    assert ask not in {x["id"] for x in rows}, (
        "an ask that was ANSWERED still reported as unanswered. This is the "
        "retracted finding: 159/159 never-acked asks at one address had "
        "replies, and calling them unread produced a false fleet-wide alarm"
    )
