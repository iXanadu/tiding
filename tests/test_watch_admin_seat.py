"""WATCH-ADMIN-SEAT-1: a shared role holds one watch claim PER BOX.

Measured 2026-10-14: `watch/admin` was one row for the whole fleet. One box's
admin held it; another box's admin attached its reader, logged "watch held by
'bridge'" every ~2 minutes, received no wakes, and its memory_status read
COVERED off the first box's claim. These tests pin the fix: each box claims its own
row, derived server-side from the listen set every deployed bridge already
sends; status answers for the asking box; the roster folds per-box claims.
"""

import json
import uuid

import pytest

from server.db import get_pool
from server.services.memory_service import (
    WATCH_SCOPE,
    WATCH_USER_ID,
    fold_shared_watches,
    watch_host_of,
)
from server.services.watch_claim import (
    WATCH_NAMESPACE,
    _claim_key,
    mint_nonce,
    watch_beat,
    watch_claim,
    watch_release,
    watch_status,
)


def _host():
    return f"wab{uuid.uuid4().hex[:6]}"


def _admin_ls(host):
    # the listen set a deployed admin bridge sends (measured on a live box)
    return ["admin", "admin@fleet", f"machine:{host}", f"admin@{host}"]


@pytest.fixture
async def clean_admin_watch(db_pool):
    async def _wipe():
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memories WHERE namespace=$1 AND scope=$2 "
                "AND user_id=$3 AND (key='watch/admin' OR key LIKE 'watch/admin@%')",
                WATCH_NAMESPACE, WATCH_SCOPE, WATCH_USER_ID)
    await _wipe()
    yield
    await _wipe()


def test_host_derived_from_listen_set():
    assert watch_host_of("admin", _admin_ls("hosta")) == "hosta"
    # admin@fleet is a fleet-wide address, never a host
    assert watch_host_of("admin", ["admin", "admin@fleet", "admin@hostc"]) == "hostc"
    assert watch_host_of("admin", ["admin", "admin@fleet"]) is None
    # explicit host is only a fallback; the listen set decides
    assert watch_host_of("admin", ["admin"], "hosta.local") == "hosta"
    assert watch_host_of("admin", _admin_ls("hosta"), "other") == "hosta"


def test_claim_key_is_per_host_only_for_shared_roles():
    assert _claim_key("admin", _admin_ls("hosta")) == "watch/admin@hosta"
    # no box readable → the pre-fix fleet-wide key, never worse than before
    assert _claim_key("admin", ["admin"]) == "watch/admin"
    # ordinary seats are untouched, even when they listen on machine:<host>
    assert _claim_key("proj-claude-2", ["proj-claude-2", "machine:hosta"]) \
        == "watch/proj-claude-2"


@pytest.mark.asyncio
async def test_two_boxes_both_granted(clean_admin_watch):
    """The bug: the second box got `held` forever."""
    a, b = _host(), _host()
    ra = await watch_claim("admin", mint_nonce(), "bridge", "/m", _admin_ls(a))
    rb = await watch_claim("admin", mint_nonce(), "bridge", "/m", _admin_ls(b))
    assert ra["verdict"] == "granted" and ra["key"] == f"watch/admin@{a}"
    assert rb["verdict"] == "granted" and rb["key"] == f"watch/admin@{b}"


@pytest.mark.asyncio
async def test_same_box_second_watcher_still_held(clean_admin_watch):
    """One watch per box is still ONE: two watchers on one box must not
    both emit (double wakes)."""
    a = _host()
    assert (await watch_claim("admin", mint_nonce(), "bridge", "/m",
                              _admin_ls(a)))["verdict"] == "granted"
    r = await watch_claim("admin", mint_nonce(), "bridge", "/m", _admin_ls(a))
    assert r["verdict"] == "held"


@pytest.mark.asyncio
async def test_beat_and_release_find_the_per_host_row(clean_admin_watch):
    a, b = _host(), _host()
    na, nb = mint_nonce(), mint_nonce()
    await watch_claim("admin", na, "bridge", "/m", _admin_ls(a))
    await watch_claim("admin", nb, "bridge", "/m", _admin_ls(b))
    assert (await watch_beat("admin", na))["verdict"] == "holder"
    assert (await watch_beat("admin", nb))["verdict"] == "holder"
    # a nonce never reaches another box's row
    assert (await watch_release("admin", mint_nonce()))["verdict"] == "not-holder"
    assert (await watch_release("admin", na))["verdict"] == "released"
    assert (await watch_status("admin", host=a))["state"] == "unheld"
    assert (await watch_status("admin", host=b))["state"] == "covered"


@pytest.mark.asyncio
async def test_prefix_row_keeps_beating_after_deploy(clean_admin_watch):
    """A watcher that claimed `watch/admin` before the deploy keeps its claim
    (its beats still land) — the deploy must not make the holder deaf."""
    a = _host()
    n = mint_nonce()
    pool = await get_pool()
    md = {"kind": "watch", "seat": "admin", "nonce": n, "armed_by": "bridge",
          "listen_set": _admin_ls(a), "host": None,
          "claimed_at": "2026-10-13T19:31:58+00:00",
          "last_beat": "2026-10-14T13:00:00+00:00"}
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memories (namespace, key, value, scope, user_id, project,"
            " tags, tags_search, metadata) VALUES ($1,'watch/admin','watch',$2,$3,"
            "'','','',$4::jsonb)",
            WATCH_NAMESPACE, WATCH_SCOPE, WATCH_USER_ID, json.dumps(md))
    assert (await watch_beat("admin", n))["verdict"] == "holder"
    # and status credits it to ITS box, read from its listen set
    s = await watch_status("admin", host=a)
    assert s["state"] == "covered" and s["host"] == a
    # while another box, deferring under the old scheme, now gets its own
    b = _host()
    assert (await watch_claim("admin", mint_nonce(), "bridge", "/m",
                              _admin_ls(b)))["verdict"] == "granted"


@pytest.mark.asyncio
async def test_status_never_lends_another_boxs_coverage(clean_admin_watch):
    """WAKE-STATUS-1's admin half: one box read COVERED off another box's claim."""
    a, b = _host(), _host()
    await watch_claim("admin", mint_nonce(), "bridge", "/m", _admin_ls(a))
    s = await watch_status("admin", host=b)
    assert s["state"] == "unheld"
    assert s["held_elsewhere"] == [a]
    assert (await watch_status("admin", host=a))["state"] == "covered"
    # an old caller with no host gets the pre-fix fleet-wide answer
    assert (await watch_status("admin"))["state"] == "covered"


def _ago(sec):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=sec)).isoformat()


def test_roster_fold_one_deaf_box_is_not_covered():
    w = {
        "admin@hosta": {"state": "covered"},
        "admin@hostb": {"state": "expired", "last_beat": _ago(200)},
        "proj-claude-2": {"state": "covered"},
    }
    out = fold_shared_watches(w, {})
    assert out["proj-claude-2"] == {"state": "covered"}
    assert out["admin"]["state"] == "expired"
    assert set(out["admin"]["by_host"]) == {"hosta", "hostb"}
    assert "admin@hosta" not in out


def test_roster_fold_prefix_row_and_new_row_same_box():
    w = {"admin": {"state": "expired", "last_beat": _ago(200)},
         "admin@hosta": {"state": "covered"}}
    metas = {"admin": {"listen_set": _admin_ls("hosta")}}
    out = fold_shared_watches(w, metas)
    assert out["admin"]["state"] == "covered"
    assert out["admin"]["by_host"] == {"hosta": {"state": "covered"}}


def test_roster_fold_drops_a_wrapped_boxs_dead_claim():
    """A box whose admin wrapped leaves an expired claim behind; it must not
    pin the role at `expired` once it is past the presence-stale window."""
    w = {"admin": {"state": "expired", "last_beat": _ago(4 * 3600)},
         "admin@hostb": {"state": "covered"}}
    metas = {"admin": {"listen_set": _admin_ls("hosta")}}
    out = fold_shared_watches(w, metas)
    assert out["admin"]["state"] == "covered"
    assert set(out["admin"]["by_host"]) == {"hostb"}



def test_roster_watch_model_keeps_by_host():
    """The fold's by_host must survive the roster response model; it was
    silently dropped (the model only had state/armed_by/last_beat)."""
    from server.models import AddressWatch
    w = AddressWatch(**{"state": "covered",
                        "by_host": {"hosta": {"state": "covered"}}})
    assert w.model_dump()["by_host"] == {"hosta": {"state": "covered"}}
    assert AddressWatch(state="unheld").model_dump()["by_host"] is None
