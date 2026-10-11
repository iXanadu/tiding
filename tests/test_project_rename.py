"""PROJECT-RENAME-1 — a project rename is a relabel plus an alias.

Shaped on the first real case (2026-10-11): one product under three old
names, each with its own ``startup/next``, merged into a fourth. What must
hold afterwards: one history under the new name with the live handoff on
top, nothing overwritten, every move reversible, and the old name still
working for both memory calls and mail — including for a session that was
already running under the old name when the rename happened.
"""
import uuid

import pytest
import pytest_asyncio

from server.services import project_alias


def _names():
    tag = uuid.uuid4().hex[:6]
    return (f"prt-a-{tag}", f"prt-b-{tag}", f"prt-c-{tag}", f"prt-new-{tag}")


async def _cleanup(db_pool, names):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE project = ANY($1::text[])", names)
        await conn.execute(
            "DELETE FROM memories WHERE scope = 'inbox' AND "
            "split_part(user_id, '@', 1) = ANY($1::text[])", names)
        await conn.execute(
            "DELETE FROM project_aliases WHERE old_name = ANY($1::text[]) "
            "OR new_name = ANY($1::text[])", names)
    await project_alias.refresh(force=True)


@pytest_asyncio.fixture
async def names(db_pool):
    n = _names()
    await _cleanup(db_pool, list(n))
    yield n
    await _cleanup(db_pool, list(n))


async def _put(client, project, key, value):
    r = await client.post("/memory/set", json={
        "namespace": "fleet", "scope": "project", "user_id": "claude-code",
        "project": project, "key": key, "value": value})
    assert r.status_code == 200, r.text


async def _get(client, project, key):
    r = await client.post("/memory/get", json={
        "namespace": "fleet", "scope": "project", "user_id": "claude-code",
        "project": project, "key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["memory"]["value"] if body.get("memory") else None


async def _relabel(client, sources, target, dry_run):
    r = await client.post("/admin/projects/relabel", json={
        "sources": sources, "target": target, "dry_run": dry_run})
    assert r.status_code == 200, r.text
    return r.json()


async def _seed(client, names):
    a, b, c, new = names
    await _put(client, a, "startup/next", "handoff A (oldest)")
    await _put(client, b, "startup/next", "handoff B (live)")
    await _put(client, b, "wip/current", "wip B")
    await _put(client, c, "startup/next", "handoff C")
    await _put(client, c, "decision/only-in-c", "decision C")
    await _put(client, a, "wip/current", "wip A")


@pytest.mark.asyncio
async def test_dry_run_reports_the_plan_and_moves_nothing(client, db_pool, names):
    a, b, c, new = names
    await _seed(client, names)
    plan = await _relabel(client, [b, c, a], new, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["moved"] == {b: 2, c: 2, a: 2}
    renamed = {(x["from_project"], x["key"]): x["new_key"] for x in plan["renamed"]}
    # Source order is precedence: b (listed first) keeps its handoff.
    assert renamed == {
        (c, "startup/next"): f"archive/{c}/startup/next",
        (a, "startup/next"): f"archive/{a}/startup/next",
        (a, "wip/current"): f"archive/{a}/wip/current",
    }
    assert await _get(client, b, "startup/next") == "handoff B (live)"
    r = await client.get("/admin/projects/aliases")
    assert all(x["old_name"] not in names for x in r.json())


@pytest.mark.asyncio
async def test_relabel_merges_keeps_everything_and_aliases(client, db_pool, names):
    a, b, c, new = names
    await _seed(client, names)
    done = await _relabel(client, [b, c, a], new, dry_run=False)
    assert done["total_moved"] == 6

    # The live handoff is on top; the others are archived, not overwritten.
    assert await _get(client, new, "startup/next") == "handoff B (live)"
    assert await _get(client, new, "wip/current") == "wip B"
    assert await _get(client, new, f"archive/{c}/startup/next") == "handoff C"
    assert await _get(client, new, f"archive/{a}/startup/next") == "handoff A (oldest)"
    assert await _get(client, new, "decision/only-in-c") == "decision C"

    # A folder whose cfg still says the old name reads and writes the new bucket.
    assert await _get(client, a, "startup/next") == "handoff B (live)"
    await _put(client, c, "session/after-rename", "written via old name")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT project, metadata FROM memories WHERE scope='project' "
            "AND key='session/after-rename' AND project = ANY($1::text[])",
            list(names))
    assert row["project"] == new

    info = (await client.get(f"/memory/project/{c}")).json()
    assert (info["project"], info["canonical"], info["renamed"]) == (c, new, True)
    assert sorted(info["old_names"]) == sorted([a, b, c])
    info_new = (await client.get(f"/memory/project/{new}")).json()
    assert sorted(info_new["old_names"]) == sorted([a, b, c])


@pytest.mark.asyncio
async def test_reverse_restores_one_source_exactly(client, db_pool, names):
    a, b, c, new = names
    await _seed(client, names)
    await _relabel(client, [b, c, a], new, dry_run=False)
    r = await client.post("/admin/projects/relabel/reverse",
                          json={"source": c, "target": new})
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == 2 and r.json()["alias_dropped"] is True
    assert await _get(client, c, "startup/next") == "handoff C"
    assert await _get(client, c, "decision/only-in-c") == "decision C"
    assert await _get(client, new, f"archive/{c}/startup/next") is None
    # The other sources stay merged.
    assert await _get(client, new, "startup/next") == "handoff B (live)"


@pytest.mark.asyncio
async def test_relabel_refuses_an_alias_as_target(client, names):
    a, b, c, new = names
    await _put(client, a, "k", "v")
    await _relabel(client, [a], b, dry_run=False)
    r = await client.post("/admin/projects/relabel", json={
        "sources": [c], "target": a, "dry_run": True})
    assert r.status_code == 422
    assert b in r.json()["detail"]


@pytest.mark.asyncio
async def test_chained_rename_flattens(client, names):
    a, b, c, new = names
    await _put(client, a, "k", "from a")
    await _relabel(client, [a], b, dry_run=False)
    await _relabel(client, [b], new, dry_run=False)
    assert project_alias.canonical(a) == new
    rows = {x["old_name"]: x["new_name"]
            for x in (await client.get("/admin/projects/aliases")).json()}
    assert rows[a] == new and rows[b] == new
    assert await _get(client, a, "k") == "from a"


@pytest.mark.asyncio
async def test_mail_to_old_name_reaches_new_and_tells_sender(client, names):
    a, b, c, new = names
    await _put(client, a, "k", "v")
    await _relabel(client, [a], new, dry_run=False)
    r = await client.post("/memory/send", json={
        "to": a, "subject": "hello", "body": "to the old name",
        "intent": "action"})
    assert r.status_code == 200, r.text
    notes = " ".join(r.json().get("recipient_warnings") or [])
    assert f"'{a}' was renamed to '{new}'" in notes
    inbox = (await client.post("/memory/inbox", json={
        "listen_set": [new], "reader_identity": f"{new}-claude-1",
        "unread_only": True})).json()["messages"]
    assert [m["subject"] for m in inbox] == ["hello"]
    assert inbox[0]["to"] == new


@pytest.mark.asyncio
async def test_session_still_on_old_name_hears_mail_to_new_name(client, names):
    # The measured failure: a reply went to the NEW name while the live
    # session, started before the cfg edit, listened only on the OLD one.
    a, b, c, new = names
    await _put(client, a, "k", "v")
    await _relabel(client, [a], new, dry_run=False)
    r = await client.post("/memory/send", json={
        "to": f"{new}@hosta", "subject": "reply", "body": "to the new name",
        "intent": "action"})
    assert r.status_code == 200, r.text
    old_session = {"listen_set": [a, f"{a}@hosta", f"{a}-claude-2"],
                   "reader_identity": f"{a}-claude-2@hosta"}
    inbox = (await client.post("/memory/inbox", json={
        **old_session, "unread_only": True})).json()["messages"]
    assert [m["subject"] for m in inbox] == ["reply"]
    w = await client.post("/memory/inbox/wait", json={
        **old_session, "timeout_seconds": 1, "since": "2000-01-01T00:00:00Z"})
    assert [m["subject"] for m in w.json()["messages"]] == ["reply"]


@pytest.mark.asyncio
async def test_unrelated_names_and_seats_are_untouched(client, names):
    a, b, c, new = names
    await _put(client, a, "k", "v")
    await _relabel(client, [a], new, dry_run=False)
    assert project_alias.canonical_address(f"{a}-claude-2") == f"{a}-claude-2"
    assert project_alias.canonical_address(f"{a}@hosta") == f"{new}@hosta"
    assert project_alias.canonical_address(b) == b
    assert await project_alias.expand_listen_set([b, f"{a}-claude-2"]) == []


@pytest.mark.asyncio
async def test_a_persons_address_is_never_aliased(client, db_pool, names):
    # The first real case had an old project name equal to the OWNER's own
    # address; aliasing it would have rerouted the owner's DMs.
    import server.services.principal_service as ps
    a, b, c, new = names
    await _put(client, a, "k", "v")
    await ps.create_principal(name=a, type="human")
    try:
        r = await client.post("/admin/projects/relabel", json={
            "sources": [a], "target": new, "dry_run": True})
        assert r.status_code == 422
        assert "person's or a login's address" in r.json()["detail"]
    finally:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM principals WHERE name = $1", a)
