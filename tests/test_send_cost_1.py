"""SEND-COST-1 — the send receipt says what the message cost.

Owner, 2026-09-22: a PM "might be helpful if they understood the cost —
message sent to 7 agents". And the overnight RETC PM sent 89 follow-ups to
workers who had not yet answered the previous one, with nothing telling it so.

Receipt facts, never a block: how many live agents the message wakes, and
whether a seat recipient has acted since the sender's previous message.
"""

import asyncio

import pytest

PFX = "sc1test"
LANE = f"{PFX}-codex"


async def _clean(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE scope IN ('inbox','presence')"
            " AND (user_id LIKE $1 OR key LIKE $2)",
            f"{PFX}%", f"presence/{PFX}%",
        )


async def _beat(client, seat, activity=True):
    r = await client.post("/memory/presence", json={
        "identity": seat, "project": PFX, "state": "running",
        "provider": "codex", "session_nonce": f"n-{seat}", "activity": activity,
    })
    assert r.status_code == 200, r.text


async def _send(client, to, intent="action", sender=f"{PFX}-codex-2@hosta"):
    r = await client.post("/memory/send", json={
        "to": to, "subject": "s", "body": "b", "from_": sender, "intent": intent,
    })
    assert r.status_code == 200, r.text
    return r.json().get("cost_warnings") or []


@pytest.mark.asyncio
async def test_a_lane_send_says_how_many_agents_it_woke(client, db_pool):
    await _clean(db_pool)
    for n in (2, 3, 4, 5):
        await _beat(client, f"{PFX}-codex-{n}")
    cost = await _send(client, LANE)
    # the sender (codex-2) is not counted
    assert any(c.startswith("Woke 3 agents") for c in cost), cost
    await _clean(db_pool)


@pytest.mark.asyncio
async def test_fyi_wakes_nobody_and_says_so(client, db_pool):
    await _clean(db_pool)
    await _beat(client, f"{PFX}-codex-3")
    cost = await _send(client, f"{PFX}-codex-3", intent="fyi")
    assert any(c.startswith("Woke 0 agents (fyi)") for c in cost), cost
    await _clean(db_pool)


@pytest.mark.asyncio
async def test_a_followup_before_the_reader_acted_is_flagged(client, db_pool):
    await _clean(db_pool)
    worker = f"{PFX}-codex-3"
    await _beat(client, worker)
    await asyncio.sleep(0.05)
    first = await _send(client, worker)
    assert not any("has not acted" in c for c in first), first
    second = await _send(client, worker)
    assert any(f"{worker} has not acted since your previous message" in c
               for c in second), second
    # A keep-alive timer beat is not the agent acting.
    await _beat(client, worker, activity=False)
    third = await _send(client, worker)
    assert any("has not acted" in c for c in third), third
    # The worker actually does something → the flag clears.
    await asyncio.sleep(0.05)
    await _beat(client, worker)
    fourth = await _send(client, worker)
    assert not any("has not acted" in c for c in fourth), fourth
    await _clean(db_pool)
