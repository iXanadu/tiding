"""Step 12: the handled-vs-read discriminator — structural, never read-state.

An ask dies only answered (answer-class reply TO IT, from another speaker)
or closed (resolve/supersede). Chatter dies by being read. Meeting traffic
(huddle/* threads) is O6's domain — no verdict. Locks: empty intent never
handles; the verdict is a STORE query on in_reply_to, not a pass over the
viewer's list window.
"""

import pytest

PFX = "s12"


async def _clear(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope='inbox' AND user_id LIKE $1",
            f"{PFX}%")


async def _send(client, to, intent=None, from_=None, from_lane=None,
                in_reply_to=None, thread_id=None):
    body = {"to": to, "body": "b", "subject": "s"}
    if intent: body["intent"] = intent
    if from_: body["from_"] = from_
    if from_lane: body["from_lane"] = from_lane
    if in_reply_to: body["in_reply_to"] = in_reply_to
    if thread_id: body["thread_id"] = thread_id
    r = await client.post("/memory/send", json=body)
    assert r.status_code == 200
    return r.json()["id"]


async def _get(client, to, msg_id, **kw):
    r = await client.post("/memory/inbox", json={
        "listen_set": [to], "reader_identity": f"{to}@x",
        "unread_only": False, **kw})
    return next(m for m in r.json()["messages"] if m["id"] == msg_id)


@pytest.mark.asyncio
async def test_answer_class_reply_to_the_ask_handles_it(client, db_pool):
    """The verdict is a STORE query: the answer lives in the ASKER's inbox
    (s12asker), yet the ask — listed from the RECIPIENT's box — reads
    handled. Lock 2's exact case."""
    await _clear(db_pool)
    ask = await _send(client, f"{PFX}worker", intent="action",
                      from_=f"{PFX}asker-claude-2", from_lane=f"{PFX}asker-claude")
    m = await _get(client, f"{PFX}worker", ask)
    assert m["handled"] is False and m["handled_via"] is None
    # The worker answers — reply lands in the ASKER's inbox, not the worker's.
    await _send(client, f"{PFX}asker", intent="action",
                from_=f"{PFX}worker-grok-3", from_lane=f"{PFX}worker-grok",
                in_reply_to=ask, thread_id=ask)
    m = await _get(client, f"{PFX}worker", ask)
    assert m["handled"] is True and m["handled_via"] == "replied"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_lock1_empty_and_fyi_replies_never_handle(client, db_pool):
    await _clear(db_pool)
    ask = await _send(client, f"{PFX}w2", intent="proceed",
                      from_=f"{PFX}a2-claude-2", from_lane=f"{PFX}a2-claude")
    await _send(client, f"{PFX}a2", from_=f"{PFX}w2-claude-2",
                from_lane=f"{PFX}w2-claude", in_reply_to=ask)   # no intent
    await _send(client, f"{PFX}a2", intent="fyi", from_=f"{PFX}w2-claude-2",
                from_lane=f"{PFX}w2-claude", in_reply_to=ask)   # got-it
    m = await _get(client, f"{PFX}w2", ask)
    assert m["handled"] is False, "a default-wake or fyi reply must not handle"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_own_followup_never_handles_and_resolve_does(client, db_pool):
    await _clear(db_pool)
    ask = await _send(client, f"{PFX}w3", intent="escalate",
                      from_=f"{PFX}a3-claude-2", from_lane=f"{PFX}a3-claude")
    # Sender nudges their own ask with answer-class intent — same speaker.
    await _send(client, f"{PFX}w3", intent="action",
                from_=f"{PFX}a3-claude-4", from_lane=f"{PFX}a3-claude",
                in_reply_to=ask)
    m = await _get(client, f"{PFX}w3", ask)
    assert m["handled"] is False, "same-lane follow-up is not an answer"
    r = await client.post(f"/memory/inbox/{ask}/resolve",
                          json={"reader_identity": f"{PFX}w3@x"})
    assert r.status_code == 200
    m = await _get(client, f"{PFX}w3", ask, include_resolved=True)
    assert m["handled"] is True and m["handled_via"] == "resolved"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_meeting_traffic_and_chatter_get_no_verdict(client, db_pool):
    await _clear(db_pool)
    meeting = await _send(client, f"{PFX}w4", intent="action",
                          from_="ixanadu", thread_id="huddle/s12room")
    chatter = await _send(client, f"{PFX}w4", intent="fyi", from_="peer")
    m = await _get(client, f"{PFX}w4", meeting)
    assert m["handled"] is None, "huddle/* threads are O6's domain"
    m = await _get(client, f"{PFX}w4", chatter)
    assert m["handled"] is None, "chatter gets no verdict"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_legacy_thread_root_fallback_and_midthread_unknown(
        client, db_pool):
    await _clear(db_pool)
    # Legacy answer: threaded to the ask, NO in_reply_to (pre-field client).
    root_ask = await _send(client, f"{PFX}w5", intent="action",
                           from_=f"{PFX}a5-claude-2",
                           from_lane=f"{PFX}a5-claude")
    await _send(client, f"{PFX}a5", intent="action",
                from_=f"{PFX}w5-grok-2", from_lane=f"{PFX}w5-grok",
                thread_id=root_ask)
    m = await _get(client, f"{PFX}w5", root_ask)
    assert m["handled"] is True and m["handled_via"] == "replied"
    # Mid-thread legacy ask: carries an older thread, no in_reply_to answers.
    mid_ask = await _send(client, f"{PFX}w5", intent="action",
                          from_=f"{PFX}a5-claude-2",
                          from_lane=f"{PFX}a5-claude",
                          thread_id="inbox/some-older-thread")
    m = await _get(client, f"{PFX}w5", mid_ask)
    assert m["handled"] is None, "mid-thread legacy is UNKNOWN, never guessed"
    await _clear(db_pool)


@pytest.mark.asyncio
async def test_unhandled_only_serves_the_sweep_view(client, db_pool):
    await _clear(db_pool)
    open_ask = await _send(client, f"{PFX}w6", intent="action",
                           from_=f"{PFX}a6-claude-2",
                           from_lane=f"{PFX}a6-claude")
    answered = await _send(client, f"{PFX}w6", intent="action",
                           from_=f"{PFX}a6-claude-2",
                           from_lane=f"{PFX}a6-claude")
    await _send(client, f"{PFX}a6", intent="action",
                from_=f"{PFX}w6-grok-2", from_lane=f"{PFX}w6-grok",
                in_reply_to=answered, thread_id=answered)
    await _send(client, f"{PFX}w6", intent="fyi", from_="peer")  # chatter
    r = await client.post("/memory/inbox", json={
        "listen_set": [f"{PFX}w6"], "reader_identity": f"{PFX}w6@x",
        "unread_only": False, "unhandled_only": True})
    ids = [m["id"] for m in r.json()["messages"]]
    assert open_ask in ids
    assert answered not in ids
    assert len(ids) == 1
    await _clear(db_pool)
