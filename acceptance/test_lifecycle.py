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
            if groups:
                # Group-addressed mail rows carry the GROUP as user_id and a
                # NULL project — a project-name marker never matches them.
                # Found as real residue on 2026-08-13: the suite passed its
                # own R-c check while five acceptteam-* rows sat in the DB.
                markers.append(groups)
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


# ═════════════════════════════════════════════════════════════════════════════
# ACCEPT-3 — CONSUMER-SIDE ARRIVAL CLAIMS (engram's slice of the matrix agreed
# in huddle DfNRCl6x, 2026-08-20, shape by agentbeast-claude-3).
#
# THE UNIT IS AN ARRIVAL CLAIM, not a test name:
#   <CONSUMER> on <WHERE>, <STATE>, receives <WHAT> sent by <PRODUCER>,
#   observed at <DESTINATION>.
# A row that cannot name its DESTINATION observation is a producer test in a
# consumer costume — that is precisely what let 2486 tests stay green through
# nine live defects. Engram owns what=DM and what=huddle wake.
#
# The `state` column is where the whole class hides. `fresh` was always tested.
# `after-restart` and `after-silence` never were, and both cost real hours on
# 2026-08-19/20.
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", PROVIDERS)
def test_arrival_channel_mail_after_restart_reaches_a_reused_name(
    provider, world, registry
):
    """CLAIM: claude on hub, AFTER-RESTART under a REUSED name, receives mail
    sent to the PROJECT CHANNEL before its predecessor died — observed at the
    successor's own default memory_inbox view.

    MEASURED IN PRODUCTION 2026-08-20, and the mechanism is narrower than
    "acks are per-reader":
      · read_by holds reader IDENTITY STRINGS, not session ids.
      · Incarnation names are REUSED, so a later session granted a previously
        used name inherits that name's acks — including acks performed by a
        session it never met.
      · R8 guards SEAT handover against inheriting open mail. Channel mail is
        NOT a seat, so R8 never applies to it.
    Net effect: mail to the project channel, acked by an earlier holder of a
    reused name, is INVISIBLE to the next holder. It is not lost, not
    unrouted, and not late — it simply never appears.

    Cost of this exact row: a 27-hour unanswered question from the owner
    (read_by ['engram-claude-3@macmini', 'engram-claude-2@macmini'] on a
    message addressed to 'engram', still status=open) and a 44-hour "urgent"
    peer ask. Both found by a hunch, not a mechanism.
    """
    proj = f"acceptprobe-estate-{provider}"
    pdir = world.project(proj)
    marker = "ESTATE ROW probe — a real ask nobody answered"
    pred_key = f"{provider}-accept-estate-pred"

    # ── predecessor takes a name and READS channel mail without resolving it.
    pred = world.session(project_dir=pdir, provider=provider,
                         session_key=pred_key)
    pred.call("memory_search", query="arm", project_dir=pdir)

    registry.send(proj, marker, "please answer this")   # to the CHANNEL
    pred_view = pred.call("memory_inbox", project_dir=pdir)
    assert marker in pred_view, (
        "precondition: the predecessor never saw the channel mail, so this "
        f"run cannot test inheritance. view: {pred_view[:300]}"
    )
    pred_ident = _listen_identity(pred_view)

    # Release so the NAME returns to the pool, then die. This is the ordinary
    # graceful path — and the one that makes the name reusable.
    registry.release(pred_key, proj)
    pred.stop()

    # ── successor claims, and must land on the SAME name for this to be a
    # test of inheritance at all.
    succ = world.session(project_dir=pdir, provider=provider,
                         session_key=f"{provider}-accept-estate-succ")
    succ.call("memory_search", query="arm", project_dir=pdir)
    view = succ.call("memory_inbox", project_dir=pdir)
    succ_ident = _listen_identity(view)

    if succ_ident != pred_ident:
        pytest.skip(
            "UNRUNNABLE, recorded not hidden: the allocator gave the successor "
            f"{succ_ident!r} rather than reusing {pred_ident!r}, so no ack was "
            "inherited and a pass here would prove nothing. Reproducing the "
            "production shape needs name reuse; see the matrix row."
        )

    # BRANCH EXPLICITLY on whether the ack was actually inherited. A single
    # `A or B` assertion here is how this row first passed with the production
    # fix REMOVED: seeing the mail directly ALSO satisfies "the successor is
    # told", so the row went green without ever exercising inheritance. Name
    # which world you are in, then assert the claim that belongs to it.
    inherited = marker not in view

    if not inherited:
        pytest.skip(
            "UNRUNNABLE, recorded not hidden: the successor sees the mail as "
            "its own unread, so no ack was inherited in this run and the "
            "estate path was never exercised. Arrival itself SUCCEEDED here — "
            "which is exactly why a bare assertion would have gone green and "
            "proved nothing. Production reproduces inheritance because a "
            "reused incarnation name carries the previous holder's read_by; "
            "this harness frees and regrants the name without that history. "
            "Row stays UNRUNNABLE until the harness can seed read_by for a "
            "prior holder of the same name."
        )

    # Inheritance DID happen: the mail is invisible, so the guidance is the
    # only thing standing between this session and a 27-hour silence.
    assert "OPEN on your addresses" in view, (
        "ARRIVAL FAIL (after-restart/channel DM): the ack was inherited, the "
        "mail is invisible in the default view, and nothing tells the reader "
        "it exists. This is the 27-hour defect exactly.\n" + view[:800]
    )
    assert "Nothing open." not in view, (
        "ARRIVAL FAIL: successor told 'Nothing open.' while mail is open on "
        "its own addresses — the guidance contradicting itself.\n" + view[:800]
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_arrival_wake_after_silence_fires_with_no_inbox_row_to_read(
    provider, world, registry
):
    """CLAIM: claude on hub, AFTER-SILENCE, receives a WAKE for a room whose
    letters are off — observed at the WATCHER's event stream, with the
    session's own inbox legitimately empty.

    THIS IS THE ROW THE READER CENSUS WAS SUPPOSED TO BE AND NEVER RAN.
    Under letters-off (10c) a room writes NO inbox rows; wakes carry it. The
    failure on 2026-08-19/20 was that a woken session checked its inbox,
    found nothing, and reported the room dead — three agents independently,
    while the room held 27 messages.

    Engram owns only the store half of this claim: that a wake reaches a
    watcher WITHOUT any inbox row existing, and that the session's empty
    inbox TELLS the reader to go read a transcript instead of concluding
    silence. Whether a given harness then surfaces the wake to its model is
    NOT observable from this tree — that row is recorded UNRUNNABLE in the
    matrix rather than marked green on store-side evidence, because marking
    it green on producer evidence is the original disease.
    """
    proj = f"acceptprobe-silence-{provider}"
    pdir = world.project(proj)

    sim = world.session(project_dir=pdir, provider=provider,
                        session_key=f"{provider}-accept-silence")
    sim.call("memory_search", query="arm", project_dir=pdir)

    # The session's inbox is EMPTY and stays empty — the letters-off shape.
    view = sim.call("memory_inbox", project_dir=pdir)
    assert "empty" in view.lower(), (
        "precondition: this row needs a genuinely empty inbox to be the "
        f"letters-off shape. view: {view[:300]}"
    )

    # DESTINATION CHECK: the empty view must not read as "nothing is
    # happening". It must point the reader at the transcript.
    assert "NOT an empty room" in view, (
        "ARRIVAL FAIL (after-silence): a session with an empty inbox is told "
        "nothing about rooms. This is the exact text that let three agents "
        "call a live room dead.\n" + view[:800]
    )
    assert "TRANSCRIPT" in view and "wake note carries" in view, (
        "ARRIVAL FAIL: the reader is told they are wrong but not WHERE to "
        "look. Telling a reader 'not here' without naming the destination "
        "only relocates the dead end.\n" + view[:800]
    )


# ═════════════════════════════════════════════════════════════════════════════
# STEP 6 — THE GATE (watch-claim v2 build order, reviewer's words: "if step 6
# fails, the protocol is theater too").
#
# CLAIM: a FRESH session, PROSE STEP DELIBERATELY SKIPPED, gets a bridge-owned
# watcher that (a) claims coverage only after a wake consumer attaches, and
# (b) delivers an ORDINAL-SEAT DM as a wake line — the rider's leg, because
# channel-only green is the coincidence already paid for once.
#
# AB absent by construction (none in this harness). The store-bounce leg of
# the gate is NOT covered here and is stated as owed rather than implied:
# the claim-API-unreachable behavior (UNHELD, loud) is unit-tested in the
# bridge suite; a real mid-run server bounce with re-claim is the remaining
# leg and runs against a disposable fleet box, not this scratch server.
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", ["claude"])
def test_step6_bridge_owned_watcher_prose_skipped_seat_dm_wakes(
    provider, world, registry
):
    import json as _json
    import os as _os
    import time as _time

    proj = f"acceptprobe-step6-{provider}"
    pdir = world.project(proj)
    key = f"{provider}-accept-step6"
    registry.purge_all_watches()

    sim = world.session(project_dir=pdir, provider=provider, session_key=key,
                        extra_env={"ENGRAM_WATCHER_POLL_INTERVAL": "1.0"})
    # ONE ordinary tool call — the thing every real session does anyway.
    # NO watcher arming, NO blessed command, NO prose step.
    sim.call("memory_search", query="arm", project_dir=pdir)
    seat = [s for s in registry.seats(proj) if s["session_key"] == key][0]["seat"]

    fifo = _os.path.join(sim.home, ".local", "state", "engram", "wake",
                         f"{key}.fifo")
    deadline = _time.monotonic() + 20
    while not _os.path.exists(fifo) and _time.monotonic() < deadline:
        _time.sleep(0.25)
    assert _os.path.exists(fifo), (
        "the bridge never created a wake FIFO — the supervisor did not start; "
        "prose-free arming is not happening"
    )

    # CLAIM-FOLLOWS-ATTACH: before any consumer exists, the seat must be
    # UNHELD — a wake stream nobody consumes must not read as coverage.
    import httpx as _httpx
    from acceptance.conftest import _AUTH
    def _watch_state():
        r = _httpx.get(f"{registry.url}/session/watch/status",
                       params={"seat": seat}, headers=_AUTH, timeout=10)
        return r.json().get("state")
    _time.sleep(2.0)
    assert _watch_state() == "unheld", (
        f"watch became {_watch_state()!r} with NO consumer attached — "
        "F4-with-extra-steps: coverage claimed for a stream nobody reads"
    )

    # The harness now plays Monitor: attach a reader.
    fd = _os.open(fifo, _os.O_RDONLY | _os.O_NONBLOCK)
    try:
        deadline = _time.monotonic() + 25
        while _watch_state() != "covered" and _time.monotonic() < deadline:
            _time.sleep(0.5)
        log_path = fifo[:-5] + ".log"
        _log = ""
        try:
            with open(log_path) as lf:
                _log = lf.read()[-1500:]
        except OSError:
            _log = "(no log file)"
        assert _watch_state() == "covered", (
            "consumer attached but the watch never claimed — the "
            f"attach→claim handoff is broken. Watcher log tail:\n{_log}"
        )

        # THE RIDER'S LEG: a DM to the ORDINAL SEAT must arrive as a wake
        # line in the FIFO. Channel-only green died at 21:57 tonight.
        mid = registry.send(seat, "step6 seat DM", "does the wake fire")
        buf = b""
        deadline = _time.monotonic() + 30
        woke = False
        while _time.monotonic() < deadline:
            try:
                chunk = _os.read(fd, 65536)
                if chunk:
                    buf += chunk
                    if mid.encode() in buf:
                        woke = True
                        break
            except BlockingIOError:
                pass
            _time.sleep(0.25)
        assert woke, (
            f"ARRIVAL FAIL: seat-addressed DM {mid} never appeared on the "
            f"wake stream within 30s. Lines seen: {buf[:400]!r}. This is the "
            "50-minute bug — the gate says the protocol is theater"
        )
        # and the line is a well-formed message line, not an event line (F11)
        line = next(l for l in buf.decode().splitlines() if mid in l)
        parsed = _json.loads(line)
        assert parsed.get("id") == mid and "event" not in parsed
    finally:
        _os.close(fd)
        # watch rows carry user_id=global / project='' — outside the R-c
        # marker sweep — so this row is purged explicitly, by key.
        registry.purge_watch(seat)
