"""ACCEPT-1: per-provider spawn-to-despawn lifecycle, ratified list v2.

Authoritative assertion list: project memory `backlog/ACCEPT-1` (2026-08-13,
adversarially reviewed by agentbeast-grok). Every test here measures a WORLD
outcome; the three review splits are load-bearing:

  · A3-register only — the picker half (A3-surface) is AB's surface and is
    NOT checkable from this tree. It is listed at the bottom as a loud
    manual step, never silently substituted by readback.
  · A9 is split crash/after-release — release DELETEs the continuity
    anchor, so same-seat-back is only promised for the crash case.
  · A2/A8 assert agreement-of-views and the stranger's clean inbox — never
    the allocator's tactic.

Providers claude/grok/codex run as bridge-level shapes (the real bridge
under that provider's env). The REAL-launcher half of each provider run is
the joint work with AgentBeast (G2). Cursor is a loud SKIP per G1
(CURSOR-IDENT-1 owner hold).
"""

from __future__ import annotations

import re

import pytest

from .driver import SessionSim, WatcherSim, cleanup_home, make_project_dir

PROVIDERS = [
    "claude",
    "grok",
    "codex",
    pytest.param(
        "cursor",
        marks=pytest.mark.skip(
            reason="G1: CURSOR-IDENT-1 owner hold — driver-spawned only; "
            "SKIPPED LOUDLY, never a silent pass"
        ),
    ),
]

_IDENT_RE = re.compile(r"(?:for|as) '?([A-Za-z0-9_.-]+)@")


def _listen_identity(inbox_text: str) -> str:
    m = _IDENT_RE.search(inbox_text)
    assert m, f"could not read the session's own listen identity from: {inbox_text[:200]}"
    return m.group(1)


@pytest.fixture()
def world(registry, tmp_path):
    """Per-test world: project dirs + sims, purged on the way out (R-c)."""
    sims: list[SessionSim] = []
    watchers: list[WatcherSim] = []
    markers: list[str] = []

    class World:
        url = registry.url

        def project(self, name: str, groups: str | None = None) -> str:
            markers.append(name)
            return make_project_dir(tmp_path, name, groups)

        def session(self, **kw) -> SessionSim:
            sim = SessionSim(server_url=registry.url, **kw)
            sims.append(sim)
            return sim.start()

        def watcher(self, sim: SessionSim) -> WatcherSim:
            w = WatcherSim(sim).start()
            watchers.append(w)
            return w

    yield World()

    for w in watchers:
        w.stop()
    for s in sims:
        try:
            s.stop()
        except Exception:
            s.kill()
        cleanup_home(s)
    for m in markers:
        registry.purge(m)
        left = registry.residue(m)
        assert left == [], (
            f"R-c violated: harness residue survived cleanup for {m!r}: {left} "
            f"— a harness that litters the register becomes the next "
            f"session's ghost"
        )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a1_a2_a3_register_one_seat_and_all_views_agree(
    provider, world, registry
):
    proj = f"acceptprobe-{provider}"
    pdir = world.project(proj)
    ident = f"{proj}-{provider}"
    key = f"{provider}-accept-{provider}-1"

    sim = world.session(
        project_dir=pdir, provider=provider, session_key=key,
        inbox_identity=ident,
    )
    # A1: ONE heartbeating tool call — deliberately not memory_whoami.
    sim.call("memory_search", query="acceptance probe", project_dir=pdir)

    seats = [s for s in registry.seats(proj) if s["session_key"] == key]
    assert len(seats) == 1, (
        f"A1: expected exactly one seat for key {key!r}; "
        f"world held {registry.seats(proj)}"
    )
    granted = seats[0]["seat"]

    # A2: AGREEMENT — readback == the identity the session actually listens on.
    inbox_text = sim.call("memory_inbox", project_dir=pdir)
    listening = _listen_identity(inbox_text)
    assert listening == granted, (
        f"A2: /session/seats says {granted!r} but the session listens as "
        f"{listening!r} — the views of 'who am I' disagree"
    )

    # A3-register: roster shows the granted name too.
    roster = sim.call("memory_roster", project=proj, project_dir=pdir)
    assert granted in roster, (
        f"A3-register: granted seat {granted!r} absent from roster: {roster[:300]}"
    )
    # A3-surface (AB picker) is NOT CHECKED HERE — see MANUAL note at bottom.

    # A6 (clean scenario): the preference was free, so ANY divergence between
    # the declared identity and the granted seat is a FAIL, not a warning.
    assert granted == ident, (
        f"A6: declared identity {ident!r} diverged from granted {granted!r} "
        f"in a scenario with no occupier — world state at claim: "
        f"{registry.seats(proj)}"
    )


@pytest.mark.parametrize("provider", ["claude"])
def test_a6_a_renamed_launch_identity_is_surfaced_failure_grade(
    provider, world, registry
):
    """Deliberate-rename scenario: the pass IS the divergence being surfaced
    failure-grade (the bridge rename banner). Banner absent = FAIL."""
    proj = f"acceptprobe-ren-{provider}"
    pdir = world.project(proj)
    ident = f"{proj}-{provider}"

    occupier = world.session(
        project_dir=pdir, provider=provider,
        session_key=f"{provider}-accept-occupier", inbox_identity=ident,
    )
    occupier.call("memory_search", query="occupy", project_dir=pdir)
    held = [s["seat"] for s in registry.seats(proj)]
    assert ident in held, f"setup failed: occupier did not take {ident!r}: {held}"

    late = world.session(
        project_dir=pdir, provider=provider,
        session_key=f"{provider}-accept-late", inbox_identity=ident,
    )
    first = late.call("memory_search", query="late claim", project_dir=pdir)
    second = late.call("memory_inbox", project_dir=pdir)

    assert "SEATED UNDER A DIFFERENT NAME" in (first + second), (
        "A6: the launch-declared identity was renamed at allocation and "
        "NOTHING SAID SO — this exact silence nearly deafened a live "
        "session on 2026-08-13. World: "
        f"{registry.seats(proj)}"
    )
    listening = _listen_identity(second)
    assert listening != ident and listening in [
        s["seat"] for s in registry.seats(proj)
    ], "the late session must actually be on the granted ordinal it was told"


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_a4_a5_dm_wakes_and_team_group_is_heard(provider, world, registry):
    proj = f"acceptprobe-wake-{provider}"
    pdir = world.project(proj, groups="acceptteam-" + provider)
    key = f"{provider}-accept-wake"

    sim = world.session(project_dir=pdir, provider=provider, session_key=key)
    sim.call("memory_search", query="arm", project_dir=pdir)
    granted = [s for s in registry.seats(proj) if s["session_key"] == key][0]["seat"]

    watcher = world.watcher(sim)
    import time
    time.sleep(2)  # let the watcher seed its backlog before the probe mail

    # A4: a DM to the granted seat wakes the session — deadline, never skip.
    msg_id = registry.send(granted, "A4 wake probe", "does the watcher fire")
    event = watcher.wait_for_event(deadline_s=25)
    assert event is not None, (
        f"A4 FAIL (R-b: timeout is FAIL, not skip): DM {msg_id} to {granted!r} "
        f"produced no watcher event within 25s"
    )
    assert event.get("id") == msg_id, (
        f"A4: watcher woke on {event.get('id')!r}, not the probe {msg_id!r}"
    )
    # Beat identity == session identity: the beat must be OUR seat's.
    seat_row = [s for s in registry.seats(proj) if s["seat"] == granted][0]
    assert seat_row.get("watcher_alive"), (
        f"A4: wake observed but the register shows no live watcher beat on "
        f"{granted!r} — a beat is only evidence if you know whose it is; "
        f"row: {seat_row}"
    )

    # A5: the folder-declared team group is heard — world observation.
    group = "acceptteam-" + provider
    gid = registry.send(group, "A5 group probe", "team address heard?")
    inbox_text = sim.call("memory_inbox", project_dir=pdir)
    assert "A5 group probe" in inbox_text, (
        f"A5: message {gid} to group {group!r} not visible to the session; "
        f"inbox: {inbox_text[:400]}"
    )


@pytest.mark.parametrize("provider", ["claude"])
def test_a7_a9_release_vs_crash_are_different_promises(
    provider, world, registry
):
    proj = f"acceptprobe-lc-{provider}"
    pdir = world.project(proj)
    key = f"{provider}-accept-lc"

    sim = world.session(project_dir=pdir, provider=provider, session_key=key)
    sim.call("memory_search", query="claim", project_dir=pdir)
    granted = [s for s in registry.seats(proj) if s["session_key"] == key][0]["seat"]

    # A9-crash: kill WITHOUT release — the row persists, continuity returns
    # the SAME seat, no ordinal burned.
    sim.kill()
    revived = world.session(project_dir=pdir, provider=provider, session_key=key)
    revived.call("memory_search", query="revive", project_dir=pdir)
    seats = [s for s in registry.seats(proj) if s["session_key"] == key]
    assert len(seats) == 1 and seats[0]["seat"] == granted, (
        f"A9-crash: same key must reclaim {granted!r}; world: {seats}"
    )

    # A7: clean stop → release → the register no longer lists it.
    revived.stop()
    released = registry.release(key, proj)
    assert released == granted, f"A7: release freed {released!r}, held {granted!r}"
    assert [s for s in registry.seats(proj) if s["session_key"] == key] == [], (
        "A7: released seat still on the register"
    )

    # A9-after-release: same key respawns — same name NOT promised (the
    # release deleted the continuity anchor); assert exactly one new seat
    # and no extra ordinal beyond the new claim.
    reborn = world.session(project_dir=pdir, provider=provider, session_key=key)
    reborn.call("memory_search", query="reborn", project_dir=pdir)
    after = registry.seats(proj)
    mine = [s for s in after if s["session_key"] == key]
    assert len(mine) == 1, f"A9-after-release: expected one seat, world: {after}"
    assert len(after) == 1, (
        f"A9-after-release: extra ordinal burned beyond the new claim: {after}"
    )


@pytest.mark.parametrize("provider", ["claude"])
def test_a8_the_stranger_never_sees_the_parked_mail(provider, world, registry):
    """Harm-based R8: whatever name the stranger is granted, the dead
    session's mail must be ABSENT from the stranger's inbox view."""
    proj = f"acceptprobe-r8-{provider}"
    pdir = world.project(proj)
    key = f"{provider}-accept-r8"
    marker_subject = "A8 parked private mail"

    # The victim MUST hold a seat DISTINCT from the project name. An unseated
    # solo session's seat IS the bare project name, which is simultaneously
    # the project GROUP address — and group-addressed mail flowing to whoever
    # serves the project next is the documented handoff pattern, not a leak.
    # The first run of this suite (2026-08-13) used an unseated victim and
    # "failed": the stranger heard the mail through the group, exactly the
    # listen_set counter-example from the ratification review. Seat-private
    # mail is only a defined concept when the seat is not a group address.
    victim = world.session(
        project_dir=pdir, provider=provider, session_key=key,
        inbox_identity=f"{proj}-victim",
    )
    victim.call("memory_search", query="claim", project_dir=pdir)
    granted = [s for s in registry.seats(proj) if s["session_key"] == key][0]["seat"]
    assert granted != proj, "setup: the victim's seat must not be the group"

    registry.send(granted, marker_subject, "for the departing session only")
    victim.stop()
    registry.release(key, proj)

    # Strongest form: the stranger explicitly REQUESTS the dead name.
    stranger = world.session(
        project_dir=pdir, provider=provider,
        session_key=f"{provider}-accept-r8-stranger",
        inbox_identity=f"{proj}-victim",
    )
    stranger.call("memory_search", query="stranger claim", project_dir=pdir)
    inbox_text = stranger.call("memory_inbox", project_dir=pdir)
    assert marker_subject not in inbox_text, (
        f"A8 FAIL: the stranger can read the dead session's mail. Stranger "
        f"listens as {_listen_identity(inbox_text)!r}; world: "
        f"{registry.seats(proj)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A3-surface — MANUAL, loudly unchecked (ratified: readback must NEVER
# substitute for this; H3's defect was a correct register behind a stale
# surface). To run it: spawn a session through AgentBeast, open the picker,
# and confirm the card shows the GRANTED name from `/session/seats`, not the
# injected preference. Owner or AB runs this half until a picker-state
# endpoint exists (H3 work, not named).
# ─────────────────────────────────────────────────────────────────────────────
