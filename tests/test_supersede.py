"""MEM-3: cross-writer correction of stale project memory.

The incident these encode (softphone, 2026-08-10): agent A wrote project
notes and left; agent B found them stale, could neither edit nor delete them,
and B's correction rows ranked BELOW the stale text at startup-sweep limits.
"""

import pytest

NS = "engram-test"
PROJECT = "supersede-proj"


async def _seed(client, key, value, user_id, tags=""):
    resp = await client.post("/memory/set", json={
        "namespace": NS, "key": key, "value": value, "scope": "project",
        "user_id": user_id, "project": PROJECT, "tags": tags,
        "expiration_days": 0,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _cleanup(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND project = $2",
            NS, PROJECT,
        )


async def _search(client, query, **kw):
    body = {"namespace": NS, "query": query, "scope": "project",
            "user_id": "*", "project": PROJECT, "limit": 10}
    body.update(kw)
    resp = await client.post("/memory/search", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


@pytest.mark.asyncio
async def test_supersede_hides_from_default_search_but_keeps_the_row(client, db_pool):
    try:
        await _seed(client, "decision/stale-thing",
                    "support report queue is empty, zero open", "grok")
        resp = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "decision/stale-thing", "project": PROJECT,
            "target_user_id": "grok",
            "reason": "measured 56 open on 08-09; this was true for hours on 07-31",
            "replacement_key": "correction/queue-not-empty",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_user_id"] == "grok"

        # Default search: gone.
        hits = await _search(client, "support report queue empty zero open")
        assert all(h["key"] != "decision/stale-thing" for h in hits)

        # History read: present, and MARKED.
        hits = await _search(client, "support report queue empty zero open",
                             include_superseded=True)
        stale = [h for h in hits if h["key"] == "decision/stale-thing"]
        assert stale and stale[0]["status"] == "superseded"
        # Value untouched — correction changes retrieval, never history.
        assert "zero open" in stale[0]["value"]
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_supersede_requires_a_reason_and_a_live_target(client, db_pool):
    try:
        await _seed(client, "decision/x", "v", "grok")
        r = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "decision/x", "project": PROJECT,
            "target_user_id": "grok", "reason": "  ",
        })
        assert r.status_code == 422  # blank reason: no audit trail, no verb

        r = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "decision/never-existed", "project": PROJECT,
            "target_user_id": "grok", "reason": "stale",
        })
        assert r.status_code == 404

        # Double-supersede: second call finds no LIVE row -> 404, not a
        # silent restamp that would overwrite the original audit fields.
        ok = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "decision/x", "project": PROJECT,
            "target_user_id": "grok", "reason": "stale"})
        assert ok.status_code == 200
        again = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "decision/x", "project": PROJECT,
            "target_user_id": "grok", "reason": "stale again"})
        assert again.status_code == 404
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_get_miss_names_the_other_writer(client, db_pool):
    """'No memory found' for a row search just returned is the measured trap."""
    try:
        await _seed(client, "session/handoff", "grok's handoff", "grok")
        resp = await client.post("/memory/get", json={
            "namespace": NS, "key": "session/handoff", "scope": "project",
            "user_id": "claude-code", "project": PROJECT,
        })
        body = resp.json()
        assert body["status"] == "not_found"
        assert any("grok" in w for w in body["partition_warnings"])
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_forget_miss_points_at_supersede(client, db_pool):
    try:
        await _seed(client, "decision/theirs", "v", "grok")
        resp = await client.post("/memory/forget", json={
            "namespace": NS, "key": "decision/theirs", "scope": "project",
            "user_id": "claude-code", "project": PROJECT,
        })
        body = resp.json()
        assert body["status"] == "not_found"
        assert any("supersede" in w for w in body["partition_warnings"])
    finally:
        await _cleanup(client, db_pool)


@pytest.mark.asyncio
async def test_store_warns_when_forking_anothers_key(client, db_pool):
    """'Stored' with no signal was how 7 keys got silently duplicated."""
    try:
        await _seed(client, "decision/lane", "grok owns CRM", "grok")
        body = await _seed(client, "decision/lane", "claude owns CRM now",
                           "claude-code")
        assert any("grok" in w and "supersede" in w
                   for w in body["partition_warnings"])
        # A plain single-writer write stays quiet.
        body = await _seed(client, "decision/only-mine", "v", "claude-code")
        assert body["partition_warnings"] == []
    finally:
        await _cleanup(client, db_pool)


# --- MEM-7: shared-scope retirement (the lesson corpus) ---------------------
# The measured gap (2026-08-15): 882 shared lessons, 0 ever superseded —
# because the verb was fixed to scope='project' and could not reach them.


async def _seed_shared(client, key, value, tags=""):
    resp = await client.post("/memory/set", json={
        "namespace": NS, "key": key, "value": value, "scope": "shared",
        "user_id": "global", "tags": tags, "expiration_days": 0,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _search_shared(client, query, **kw):
    body = {"namespace": NS, "query": query, "scope": "shared",
            "user_id": "global", "limit": 10}
    body.update(kw)
    resp = await client.post("/memory/search", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


async def _cleanup_shared(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND scope = 'shared'",
            NS,
        )


@pytest.mark.asyncio
async def test_shared_supersede_drains_search_and_keeps_the_row(client, db_pool):
    try:
        await _seed_shared(client, "lesson/dead-tech",
                           "always warm the ollama kv cache before batch runs")
        resp = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "lesson/dead-tech", "scope": "shared",
            "target_user_id": "global",
            "reason": "ollama dropped fleet-wide 2026-03; nothing warms it",
            "replacement_key": "lesson/embeddings-in-process",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_user_id"] == "global"

        # Default shared search: drained.
        hits = await _search_shared(client, "warm ollama kv cache batch")
        assert all(h["key"] != "lesson/dead-tech" for h in hits)

        # History read: present, marked, value verbatim.
        hits = await _search_shared(client, "warm ollama kv cache batch",
                                    include_superseded=True)
        stale = [h for h in hits if h["key"] == "lesson/dead-tech"]
        assert stale and stale[0]["status"] == "superseded"
        assert "ollama" in stale[0]["value"]

        # Double-supersede: no silent restamp of the audit fields.
        again = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "lesson/dead-tech", "scope": "shared",
            "target_user_id": "global", "reason": "stale again"})
        assert again.status_code == 404
    finally:
        await _cleanup_shared(client, db_pool)


@pytest.mark.asyncio
async def test_shared_supersede_ignores_the_callers_project(client, db_pool):
    """Shared rows carry no project; a caller's resolved project must not
    make the match miss (the bridge always resolves one)."""
    try:
        await _seed_shared(client, "lesson/projectless", "v")
        resp = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "lesson/projectless", "scope": "shared",
            "project": "some-project", "target_user_id": "global",
            "reason": "retired in a test",
        })
        assert resp.status_code == 200, resp.text
    finally:
        await _cleanup_shared(client, db_pool)


@pytest.mark.asyncio
async def test_supersede_rejects_personal_scopes(client, db_pool):
    for bad in ("user", "machine"):
        resp = await client.post("/memory/supersede", json={
            "namespace": NS, "key": "k", "scope": bad,
            "target_user_id": "someone", "reason": "no verb over personal scopes",
        })
        assert resp.status_code == 422, (bad, resp.text)


@pytest.mark.asyncio
async def test_forget_success_names_surviving_siblings(client, db_pool):
    """Deleting your own row under a shared key must say the key lives on.

    The measured trap: an agent used forget as a LOOKUP on a shared key and
    silently destroyed its own correction, receiving plain success.
    """
    try:
        await _seed(client, "decision/shared", "grok's stale claim", "grok")
        await _seed(client, "decision/shared", "claude's correction", "claude-code")
        resp = await client.post("/memory/forget", json={
            "namespace": NS, "key": "decision/shared", "scope": "project",
            "user_id": "claude-code", "project": PROJECT,
        })
        body = resp.json()
        assert body["status"] == "ok"
        assert any("STILL exists" in w and "grok" in w
                   for w in body["partition_warnings"])
        # Deleting a key with NO siblings stays quiet.
        await _seed(client, "decision/solo", "v", "claude-code")
        resp = await client.post("/memory/forget", json={
            "namespace": NS, "key": "decision/solo", "scope": "project",
            "user_id": "claude-code", "project": PROJECT,
        })
        assert resp.json()["partition_warnings"] == []
    finally:
        await _cleanup(client, db_pool)
