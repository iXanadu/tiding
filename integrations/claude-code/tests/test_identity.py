"""Tests for inbox identity computation — including the per-session override."""

import os

import engram_mcp.identity as identity
from engram_mcp.identity import compute_identity


def _host(monkeypatch):
    monkeypatch.setattr(identity, "hostname", lambda: "macmini")


def test_default_identity_is_project_derived(monkeypatch):
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    reader, listen_set = compute_identity("/whatever")
    assert reader == "beastchat@macmini"
    assert listen_set == ["beastchat", "machine:macmini", "beastchat@macmini"]


def test_identity_from_engram_cfg_when_no_env(monkeypatch):
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    # file-driven: .engram.cfg declares inbox_identity, no env var set
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: "beastchat-app")
    reader, listen_set = compute_identity("/whatever")
    assert reader == "beastchat-app@macmini"
    assert listen_set == [
        "beastchat-app",
        "beastchat",
        "beastchat@macmini",
        "machine:macmini",
        "beastchat-app@macmini",
    ]


def test_env_var_wins_over_engram_cfg(monkeypatch):
    _host(monkeypatch)
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: "from-file")
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "from-env")
    reader, _ = compute_identity("/whatever")
    assert reader == "from-env@macmini"


def test_override_gives_distinct_identity_but_keeps_project_group(monkeypatch):
    _host(monkeypatch)
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "beastchat-app")
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    reader, listen_set = compute_identity("/whatever")
    # precise per-session identity for DMs + self-filter precision...
    assert reader == "beastchat-app@macmini"
    # ...but still listens on the shared project group for broadcasts
    assert "beastchat" in listen_set
    # ...and on the project's HOST-QUALIFIED group address, which names "the
    # beastchat session on macmini" without knowing its seat.
    assert listen_set == [
        "beastchat-app",
        "beastchat",
        "beastchat@macmini",
        "machine:macmini",
        "beastchat-app@macmini",
    ]


def test_seated_session_still_answers_to_project_at_host(monkeypatch):
    """A launcher-assigned seat must not cost a session its `<project>@<host>`.

    The operator addresses maintenance sessions as `admin@webone` and
    `admin@macmini` — one per box, distinct, and knowable without asking what
    seat a launcher happened to inject. That address is this module's
    documented contract (see the header) and an UNSEATED session has always
    had it.

    A SEATED session did not, until 2026-08-06: the override branch added the
    seat's qualified form and dropped the project's. When the launcher began
    seating every session it spawned, `admin@<host>` silently ceased to exist
    fleet-wide — no rejection, no error, simply no listener. This test is the
    regression guard for that address, expressed in the operator's own terms.
    """
    _host(monkeypatch)
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "maintenance-claude-webone")
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "admin")
    reader, listen_set = compute_identity("/whatever")

    # The seat still names this session precisely — seats are not weakened.
    assert reader == "maintenance-claude-webone@macmini"
    assert "maintenance-claude-webone" in listen_set

    # ...and the operator's convention answers again.
    assert "admin@macmini" in listen_set

    # The group address is NOT a substitute: it reaches every box, so it
    # cannot distinguish the macmini session from the webone one. That
    # inability is the whole reason the host-qualified form exists.
    assert "admin" in listen_set


def test_two_boxes_get_distinct_project_at_host_addresses(monkeypatch):
    """`admin@macmini` and `admin@webone` must name DIFFERENT sessions.

    The operator's requirement is that a sender/receiver id be unique and say
    who it is and where it is. Two seated maintenance sessions on two boxes
    are the case that failed today, so assert it directly rather than trusting
    that per-host formatting implies per-host distinctness.
    """
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "maintenance-claude")
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "admin")

    monkeypatch.setattr(identity, "hostname", lambda: "macmini")
    _, on_macmini = compute_identity("/whatever")
    monkeypatch.setattr(identity, "hostname", lambda: "webone")
    _, on_webone = compute_identity("/whatever")

    assert "admin@macmini" in on_macmini
    assert "admin@webone" in on_webone
    # Neither box answers to the other's address — that is what makes the
    # convention addressable rather than ambiguous.
    assert "admin@webone" not in on_macmini
    assert "admin@macmini" not in on_webone


def test_override_equal_to_project_is_a_noop(monkeypatch):
    _host(monkeypatch)
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "beastchat")
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    reader, listen_set = compute_identity("/whatever")
    assert reader == "beastchat@macmini"
    assert listen_set == ["beastchat", "machine:macmini", "beastchat@macmini"]


def test_omitted_project_dir_recalls_the_pinned_identity(monkeypatch):
    """The read/write divergence bug: a call that omits project_dir must NOT
    fall back to admin once the session has an established identity.

    Reproduces projbeta/projalpha/admin@macmini (2026-07-18): read as the
    project, write as admin. With the pin, the write recalls the project.
    """
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)

    def _derive(project_dir):
        # Mirror production: a usable project path resolves to its project,
        # an empty/None one falls back to admin.
        return "projbeta" if project_dir else identity.ADMIN_NAME

    monkeypatch.setattr(identity, "derive_project_name", _derive)

    # Read path passes project_dir → pins projbeta.
    read_reader, _ = compute_identity("/Users/ixanadu/projects/ProjBeta")
    assert read_reader == "projbeta@macmini"

    # Write path omits project_dir → recalls the pin instead of going admin.
    write_reader, write_set = compute_identity(None)
    assert write_reader == "projbeta@macmini"
    assert write_set == ["projbeta", "machine:macmini", "projbeta@macmini"]


def test_cold_session_without_project_dir_or_cwd_falls_back_to_admin(monkeypatch):
    """No explicit arg, no pin, no startup cwd → pre-anchor default (admin).

    (conftest neutralizes _STARTUP_CWD to None so this isolates the true
    nothing-to-anchor-to case.)
    """
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: "proj" if project_dir else identity.ADMIN_NAME,
    )
    reader, _ = compute_identity(None)
    assert reader == "admin@macmini"


def test_startup_cwd_anchors_identity_when_project_dir_omitted(monkeypatch):
    """The durable fix: an omitted project_dir resolves from the bridge's spawn
    cwd (its project root), so identity never silently drops to admin.

    derive_project_name is left REAL-ish here (delegating to .engram.cfg via a
    stub) to show cwd is only the ANCHOR — the declared name is what wins.
    """
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    # cwd = the session's project root; .engram.cfg there declares 'projbeta'
    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu/projects/ProjBeta")
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: "projbeta"
        if project_dir == "/Users/ixanadu/projects/ProjBeta"
        else identity.ADMIN_NAME,
    )
    reader, listen_set = compute_identity(None)
    assert reader == "projbeta@macmini"
    assert listen_set == ["projbeta", "machine:macmini", "projbeta@macmini"]


def test_cross_project_call_does_not_move_identity(monkeypatch):
    """A call scoped to another project must NOT re-derive who this session is.

    This assertion is INVERTED from what it was before 2026-08-06, and the
    inversion is the fix. It previously read "an explicit arg still wins over
    the cwd anchor (admin cross-project work, ...)" — encoding, as a
    requirement, the very thing that was broken: reading a peer's project
    silently changed the session's own addresses for the rest of its life.

    `project_dir` on a call answers "which project's memory am I reading?".
    It must never answer "who am I?". The cwd anchor does that.
    """
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu/projects/Alpha")
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: (project_dir or "").rsplit("/", 1)[-1].lower()
        if project_dir
        else identity.ADMIN_NAME,
    )
    reader, _ = compute_identity("/Users/ixanadu/projects/Beta")
    assert reader == "alpha@macmini"


def test_one_cross_project_call_does_not_cost_a_session_its_group(monkeypatch):
    """The measured 2026-08-06 disturbance, reproduced end to end.

    A maintenance session anchored at ~/maintenance (project `admin`) makes ONE
    memory call scoped to another project — ordinary work — and then a normal
    call with the argument omitted. Before the fix it lost BOTH `admin` and
    `admin@macmini` from that point on, permanently.

    The failure was silent in the worst way: the watcher is anchored by a flag,
    so it never moved and still WOKE the session on mail to `admin@macmini` —
    the session then read an inbox whose listen_set no longer contained the
    address the mail was addressed to. Woken, and unable to find it.
    """
    _host(monkeypatch)
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "maintenance-claude-macmini")
    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu/maintenance")
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: "admin"
        if (project_dir or "").endswith("maintenance")
        else "engram",
    )

    _, at_start = compute_identity("/Users/ixanadu/maintenance")
    assert "admin" in at_start and "admin@macmini" in at_start

    # One cross-project read — legitimate, and must not touch identity.
    compute_identity("/Users/ixanadu/projects/engram")

    # ...and the next ordinary call, argument omitted.
    _, after = compute_identity(None)
    assert "admin@macmini" in after, "lost its host-qualified address"
    assert "admin" in after, "lost its own project group — broadcasts stop landing"
    assert "engram@macmini" not in after, "adopted another project's address"


def test_watcher_anchor_wins_over_its_launching_shell_cwd(monkeypatch):
    """The watcher's `--project-dir` must beat the cwd of whatever shell ran it.

    The bridge and the watcher have DIFFERENT authoritative anchors: the bridge
    is spawned with cwd = the session's project root, while the watcher inherits
    whatever directory its launching shell happened to be in. If the watcher
    resolved identity from cwd it would listen as the wrong session — the
    WATCH-1 failure, silent because project mail still lands.
    """
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setattr(identity, "_STARTUP_CWD", "/Users/ixanadu")  # a bare shell
    monkeypatch.setattr(identity, "_IDENTITY_ANCHOR", None)
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: (project_dir or "").rsplit("/", 1)[-1].lower()
        if project_dir
        else identity.ADMIN_NAME,
    )
    identity.set_identity_anchor("/Users/ixanadu/projects/Beta")
    reader, _ = compute_identity("/Users/ixanadu/projects/Beta")
    assert reader == "beta@macmini"


def test_explicit_project_dir_repins_last_wins(monkeypatch):
    """An explicit usable project_dir always wins and updates the pin."""
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: (project_dir or "").rsplit("/", 1)[-1].lower()
        if project_dir
        else identity.ADMIN_NAME,
    )
    first, _ = compute_identity("/Users/ixanadu/projects/Alpha")
    assert first == "alpha@macmini"
    second, _ = compute_identity("/Users/ixanadu/projects/Beta")
    assert second == "beta@macmini"
    # Omitting now recalls the most recent explicit pin (beta), not alpha.
    recalled, _ = compute_identity("")
    assert recalled == "beta@macmini"


def test_relative_project_dir_does_not_pin(monkeypatch):
    """A relative path is not a usable identity source — must not become the
    pin, and must not be honored over an existing pin."""
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setattr(
        identity,
        "derive_project_name",
        lambda project_dir: "engram" if project_dir == "/Users/ixanadu/projects/engram"
        else identity.ADMIN_NAME,
    )
    compute_identity("/Users/ixanadu/projects/engram")  # pin engram
    reader, _ = compute_identity("relative/path")  # not absolute → recalls pin
    assert reader == "engram@macmini"


def test_two_siblings_get_distinct_identities_sharing_a_group(monkeypatch):
    _host(monkeypatch)
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")

    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "beastchat-server")
    srv_reader, srv_set = compute_identity("/whatever")
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "beastchat-app")
    app_reader, app_set = compute_identity("/whatever")

    assert srv_reader != app_reader
    # both still share the project group address
    assert "beastchat" in srv_set and "beastchat" in app_set
    # each can be addressed precisely without hitting the other
    assert "beastchat-server@macmini" in srv_set
    assert "beastchat-server@macmini" not in app_set


# --- ENGRAM_CHANNELS channel-join (MSG-5 bridge half) -----------------------

class TestChannelJoin:
    def test_channels_appended_to_listen_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ENGRAM_CHANNELS", "#devagents,#fleet")
        from engram_mcp.identity import compute_identity
        reader, listen = compute_identity(str(tmp_path / "projects" / "myproj"))
        assert "#devagents" in listen and "#fleet" in listen
        # channels ride along; core addresses unchanged
        assert "myproj" in listen and reader.startswith("myproj@")

    def test_sigil_required_bare_names_dropped(self, monkeypatch, tmp_path):
        """A bare name is a PROJECT address — never silently promoted."""
        monkeypatch.setenv("ENGRAM_CHANNELS", "devagents, #ok, #, ,#dup,#dup")
        from engram_mcp.identity import resolve_channels
        assert resolve_channels() == ["#ok", "#dup"]

    def test_unset_env_changes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ENGRAM_CHANNELS", raising=False)
        from engram_mcp.identity import compute_identity
        _, listen = compute_identity(str(tmp_path / "projects" / "myproj"))
        assert not any(a.startswith("#") for a in listen)

    def test_channels_with_identity_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ENGRAM_CHANNELS", "#courseware")
        monkeypatch.setenv("ENGRAM_INBOX_IDENTITY", "myproj-grok")
        from engram_mcp.identity import compute_identity
        reader, listen = compute_identity(str(tmp_path / "projects" / "myproj"))
        assert reader.startswith("myproj-grok@")
        assert "#courseware" in listen and "myproj" in listen


class TestResolveProvider:
    """ENGRAM_PROVIDER — launch-injected, because the bridge is provider-neutral.

    Regression guard for the 2026-07-23 hardcode: server.py passed a literal
    ``provider="claude"`` to every presence heartbeat, so the roster and the
    seat-collision ``providers_seen`` detail could not distinguish a Grok
    session from a Claude one — including on a real cross-provider collision,
    the exact case that field exists to disambiguate.
    """

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_PROVIDER", "grok")
        from engram_mcp.identity import resolve_provider
        assert resolve_provider() == "grok"

    def test_defaults_to_claude_for_back_compat(self, monkeypatch):
        # Unset must reproduce the pre-fix hardcode exactly, so sessions
        # launched by a launcher that predates provider support are unchanged.
        monkeypatch.delenv("ENGRAM_PROVIDER", raising=False)
        from engram_mcp.identity import resolve_provider
        assert resolve_provider() == "claude"

    def test_normalizes_case_and_whitespace(self, monkeypatch):
        # The roster is read by humans and matched by agents; a provider that
        # only compares equal after normalization is a provider nobody can filter on.
        monkeypatch.setenv("ENGRAM_PROVIDER", "  GROK  ")
        from engram_mcp.identity import resolve_provider
        assert resolve_provider() == "grok"

    def test_empty_falls_back_rather_than_reporting_blank(self, monkeypatch):
        monkeypatch.setenv("ENGRAM_PROVIDER", "   ")
        from engram_mcp.identity import resolve_provider
        assert resolve_provider() == "claude"

    def test_heartbeat_sends_resolved_provider_not_a_literal(self, monkeypatch):
        """Assert the CALL SITE, not just the helper.

        The lesson from the SEAT-1 exchange (2026-07-23): a fully-working
        mechanism that nothing invokes is indistinguishable from a broken one,
        and a suite that only tests the helper stays green through the outage.
        """
        import asyncio
        import engram_mcp.server as srv

        monkeypatch.setenv("ENGRAM_PROVIDER", "grok")
        srv._last_heartbeat = 0.0
        captured = {}

        async def fake_presence_update(**kwargs):
            captured.update(kwargs)
            return {"collision": None}

        monkeypatch.setattr(srv._client, "presence_update", fake_presence_update)
        asyncio.run(srv._heartbeat("/tmp/whatever"))
        assert captured.get("provider") == "grok"


class TestRuntimeSeat:
    """Seats taken mid-session, for sessions no launcher seated.

    Launch-time injection stays the strongest mechanism, but it only covers
    sessions a launcher spawned. A session opened by hand cannot re-exec
    itself, and .engram.cfg is per-FOLDER — useless for the one case that
    matters, two sessions sharing one folder.
    """

    def setup_method(self):
        from engram_mcp.identity import clear_seat
        clear_seat()

    teardown_method = setup_method

    def test_runtime_seat_becomes_the_identity(self, monkeypatch):
        _host(monkeypatch)
        monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
        identity.take_seat("meidura-audit")
        reader, listen = compute_identity("/whatever")
        assert reader == "meidura-audit@macmini"
        assert listen == [
            "meidura-audit",
            "meidura",
            "meidura@macmini",
            "machine:macmini",
            "meidura-audit@macmini",
        ]

    def test_project_group_survives_so_broadcasts_still_land(self, monkeypatch):
        """The point of a seat is a PRIVATE address in addition to the shared
        one — not instead of it. Losing the group would make a seated session
        unreachable by project-wide announcements."""
        _host(monkeypatch)
        monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
        identity.take_seat("meidura-audit")
        _, listen = compute_identity("/whatever")
        assert "meidura" in listen

    def test_runtime_outranks_launch_env(self, monkeypatch):
        """A seat taken mid-flight is strictly newer information than whatever
        the spawn assumed."""
        _host(monkeypatch)
        monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "meidura-claude")
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
        identity.take_seat("meidura-remediate")
        reader, _ = compute_identity("/whatever")
        assert reader == "meidura-remediate@macmini"

    def test_normalizes_at_the_boundary(self, monkeypatch):
        """Seats are matched by exact string against listen_sets; one that only
        compares equal after someone remembers to lowercase it is a seat that
        silently receives no mail."""
        assert identity.take_seat("  Meidura-AUDIT  ") == "meidura-audit"

    def test_empty_seat_is_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            identity.take_seat("   ")

    def test_unset_seat_changes_nothing(self, monkeypatch):
        _host(monkeypatch)
        monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
        reader, listen = compute_identity("/whatever")
        assert reader == "meidura@macmini"
        assert listen == ["meidura", "machine:macmini", "meidura@macmini"]


class TestSeatFile:
    """SEAT-2 — the seat file that makes a runtime re-seat safe.

    A runtime seat moves the bridge instantly, but the watcher is a separate
    process that resolved identity at start. "Remember to re-arm your watcher"
    is discipline; a file both processes resolve from is inheritance.
    """

    def setup_method(self):
        from engram_mcp.identity import clear_seat
        clear_seat()

    teardown_method = setup_method

    def test_no_launcher_key_still_gets_a_seat_file(self, monkeypatch):
        """SEAT-3 reverses the old "no launcher, no seat file" contract.

        Previously a hand-launched session got no session key, so no seat file,
        so no way for its watcher to follow a re-seat — the structural
        guarantee was reserved for launcher-spawned sessions. The key is now
        DERIVED from the harness parent process, so the guarantee is universal.
        """
        monkeypatch.delenv(identity.SESSION_KEY_ENV, raising=False)
        path = identity.seat_file_path()
        assert path is not None
        assert "auto-" in path

    def test_no_resolvable_key_means_no_file(self, monkeypatch):
        """The genuine fallback: nothing to key on at all.

        Must degrade to exactly the pre-SEAT-3 behaviour rather than raise —
        a session whose process tree cannot be read still has to start.
        """
        monkeypatch.delenv(identity.SESSION_KEY_ENV, raising=False)
        monkeypatch.setattr(identity, "derive_session_key", lambda: None)
        assert identity.seat_file_path() is None
        assert identity.read_seat_file() is None

    def test_path_is_keyed_on_session_not_project(self, monkeypatch):
        """Two sessions in ONE project folder is the whole case — a
        project-keyed path would collide exactly where it must not."""
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-ab-meidura")
        a = identity.seat_file_path()
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "grok-1f6a3406efd4")
        b = identity.seat_file_path()
        assert a != b and a and b

    def test_take_seat_writes_it_and_a_peer_reads_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-testkey")
        monkeypatch.setenv("HOME", str(tmp_path))
        identity.take_seat("meidura-audit")
        assert identity.read_seat_file() == "meidura-audit"

    def test_watcher_sees_a_reseat_without_restarting(self, monkeypatch, tmp_path):
        """The point of SEAT-2: a peer process re-resolves the new seat with no
        in-process state and no re-arm."""
        _host(monkeypatch)
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-testkey2")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)

        before, _ = compute_identity("/whatever")
        assert before == "meidura@macmini"

        identity.take_seat("meidura-audit")      # the "bridge" re-seats
        identity.clear_seat()                    # simulate the separate watcher process
        after, listen = compute_identity("/whatever")
        assert after == "meidura-audit@macmini"
        assert "meidura" in listen, "project group must survive the re-seat"

    def test_malformed_file_returns_none_and_never_raises(self, monkeypatch, tmp_path):
        """A watcher that dies on a bad seat file is worse than one on a stale
        seat: the stale one still catches project-addressed mail."""
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-bad")
        monkeypatch.setenv("HOME", str(tmp_path))
        path = identity.seat_file_path()
        import os as _os
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        for junk in ["", "   ", "two words", "\x00\xff"]:
            with open(path, "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(junk)
            assert identity.read_seat_file() is None

    def test_unreadable_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-missing")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert identity.read_seat_file() is None  # never created

    def test_seat_file_outranks_launch_env(self, monkeypatch, tmp_path):
        """A seat recorded this session is newer information than the spawn's."""
        _host(monkeypatch)
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-testkey3")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "meidura-claude")
        monkeypatch.setattr(identity, "derive_project_name", lambda _d: "meidura")
        monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
        identity.take_seat("meidura-remediate")
        identity.clear_seat()
        reader, _ = compute_identity("/whatever")
        assert reader == "meidura-remediate@macmini"

    def test_whitespace_seat_is_rejected(self, monkeypatch):
        import pytest
        with pytest.raises(ValueError):
            identity.take_seat("two words")


class TestAutoSessionKey:
    """SEAT-3: a session key without a launcher.

    Launch-time injection only ever covered sessions a launcher started. A
    session opened by hand — the common case when someone decides AFTER
    starting that two agents should co-work in one folder — had no key, so no
    seat file, so its watcher could not follow a re-seat.
    """

    def setup_method(self):
        identity.clear_seat()
        identity._DISCOVERED_SEAT_PATH = None

    teardown_method = setup_method

    def test_bridge_keys_on_its_harness_parent(self, monkeypatch):
        """The harness is the bridge's PARENT by construction: a stdio MCP
        server is spawned as a direct child of the harness that speaks to it."""
        monkeypatch.setattr(identity.os, "getppid", lambda: 4813)
        monkeypatch.setattr(
            identity, "_proc_info", lambda pid: (4812, "Fri Jul 24 10:29:35 2026")
        )
        key = identity.derive_session_key()
        assert key == "auto-" + identity.hostname() + "-4813-fri-jul-24-10-29-35-2026"

    def test_start_time_defeats_pid_reuse(self):
        """A recycled pid must yield a DIFFERENT key, not silently inherit a
        dead session's seat."""
        a = identity.auto_session_key_for(4813, "Fri Jul 24 10:29:35 2026", "macmini")
        b = identity.auto_session_key_for(4813, "Fri Jul 24 18:02:11 2026", "macmini")
        assert a != b

    def test_launcher_key_still_wins(self, monkeypatch):
        """A launcher's key is preferred: it survives a respawn, a pid cannot."""
        monkeypatch.setenv(identity.SESSION_KEY_ENV, "claude-ab-engram")
        assert identity.resolve_session_key() == "claude-ab-engram"

    def test_generated_keys_carry_the_instability_marker(self):
        """SEAT-16: a generated key names a PROCESS, and must say so.

        The `auto-` prefix is the contract a consumer reads to tell a
        process-lifetime key from a launcher-injected stable one — the
        distinction the server serves back as `session_key_generated`.
        Asserted against the CONSTANT so the marker and the minting can
        never drift apart silently.
        """
        key = identity.auto_session_key_for(99, "Mon Aug 10 09:00:00 2026", "box")
        assert key.startswith(identity.AUTO_KEY_PREFIX)

    def test_orphan_has_no_derived_key(self, monkeypatch):
        """ppid <= 1 means no harness to anchor to — degrade, never guess."""
        monkeypatch.setattr(identity.os, "getppid", lambda: 1)
        assert identity.derive_session_key() is None

    def test_watcher_discovers_the_bridge_seat_by_walking_ancestors(
        self, monkeypatch, tmp_path
    ):
        """The hand-launched case end to end.

        The bridge names its seat file after the HARNESS (its own parent). The
        watcher is a deeper descendant — watcher -> shell -> harness — so its
        own derived key names the SHELL and finds nothing. Walking up, it
        reaches the harness and finds the file the bridge wrote.
        """
        monkeypatch.setenv(identity.SEATS_DIR_ENV, str(tmp_path / "seats"))
        monkeypatch.delenv(identity.SESSION_KEY_ENV, raising=False)
        monkeypatch.setattr(identity, "hostname", lambda: "macmini")

        # watcher(6177) -> zsh(6175) -> harness(4813) -> tmux server(4812)
        tree = {
            6177: (6175, "Fri Jul 24 10:42:49 2026"),
            6175: (4813, "Fri Jul 24 10:42:49 2026"),
            4813: (4812, "Fri Jul 24 10:29:35 2026"),
            4812: (1, "Fri Jul 24 10:29:35 2026"),
        }
        monkeypatch.setattr(identity, "_proc_info", lambda pid: tree.get(pid))
        monkeypatch.setattr(identity.os, "getpid", lambda: 6177)

        # The bridge wrote its seat under the HARNESS pid.
        harness_key = identity.auto_session_key_for(
            4813, tree[4813][1], "macmini"
        )
        path = identity.seat_file_path(harness_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("engram-claude-2\n")

        assert identity.discover_seat_file() == path
        assert identity.read_seat_file() == "engram-claude-2"

    def test_discovery_stops_at_the_nearest_ancestor(self, monkeypatch, tmp_path):
        """Nearest wins, which is the right answer under nested harnesses —
        and keeps the walk from reaching the tmux server, which is SHARED by
        every session on the box and would hand them all one key."""
        monkeypatch.setenv(identity.SEATS_DIR_ENV, str(tmp_path / "seats"))
        monkeypatch.delenv(identity.SESSION_KEY_ENV, raising=False)
        monkeypatch.setattr(identity, "hostname", lambda: "macmini")
        tree = {
            10: (20, "start-a"),
            20: (30, "start-b"),
            30: (1, "start-c"),
        }
        monkeypatch.setattr(identity, "_proc_info", lambda pid: tree.get(pid))
        monkeypatch.setattr(identity.os, "getpid", lambda: 10)

        near = identity.seat_file_path(
            identity.auto_session_key_for(20, "start-b", "macmini")
        )
        far = identity.seat_file_path(
            identity.auto_session_key_for(30, "start-c", "macmini")
        )
        os.makedirs(os.path.dirname(near), exist_ok=True)
        for p, seat in ((near, "near-seat"), (far, "far-seat")):
            with open(p, "w") as fh:
                fh.write(seat + "\n")

        assert identity.read_seat_file() == "near-seat"


class TestAdminFallbackVisibility:
    """ID-1: 'admin' by declaration is a choice; 'admin' by fallthrough is a
    fact that must be visible. Two resolvers of different strictness answer
    "what project is this?" — memory raises on an unconfigured dir, addressing
    silently adopted the administrator's identity. The predicate below is how
    the tool layer tells the two apart."""

    def test_scratch_dir_is_fallback(self, tmp_path):
        from engram_mcp.identity import admin_was_fallback
        assert admin_was_fallback(str(tmp_path)) is True

    def test_no_dir_is_fallback(self):
        from engram_mcp.identity import admin_was_fallback, reset_session_pin
        reset_session_pin()
        assert admin_was_fallback(None) is True

    def test_projects_path_is_not_admin_at_all(self):
        from engram_mcp.identity import admin_was_fallback
        assert admin_was_fallback("/Users/x/projects/widget") is False

    def test_declared_admin_is_a_choice_not_a_fallback(self, tmp_path):
        from engram_mcp.identity import admin_was_fallback, derive_project_name
        (tmp_path / ".engram.cfg").write_text("project = admin\n")
        assert derive_project_name(str(tmp_path)) == "admin"
        assert admin_was_fallback(str(tmp_path)) is False


# --- The goodbye: identifying the SESSION process (2026-08-01) -----------


def test_bridge_records_and_watcher_rediscovers_the_session(monkeypatch, tmp_path):
    """The bridge writes down what only it can know; the watcher reads it.

    Only the bridge can identify the session, because its parent IS the
    harness by construction. The watcher's parent is a shell wrapper in a
    different process group, so it must be told.
    """
    _host(monkeypatch)
    monkeypatch.setattr(identity, "seat_file_path",
                        lambda key=None: str(tmp_path / "k.seat"))
    written = identity.record_session_process()
    assert written is not None
    assert identity.discover_session_process() == (
        os.getppid(), identity._proc_info(os.getppid())[1]
    )


def test_a_recycled_pid_is_not_the_same_process(monkeypatch):
    """A PID ALONE IS AN ADDRESS, NOT AN IDENTITY.

    The OS recycles pids. Matching on the number alone would report a stranger
    as our still-living session forever — and since only a DEAD session is
    reported, that error runs in the safe direction here. The dangerous mirror
    is the one this pins: the start time must make a recycled pid read as gone
    rather than as continued life.
    """
    me = os.getpid()
    start = identity._proc_info(me)[1]
    assert identity.process_is_gone(me, start) is False
    assert identity.process_is_gone(me, "Thu Jan  1 00:00:00 2020") is True


def test_no_recorded_session_is_unknown_never_dead(monkeypatch, tmp_path):
    """"I could not identify a session" must never collapse into "it died".

    A watcher that cannot find its session simply never reports a farewell.
    Returning something falsy that a caller might read as a death would
    manufacture one for every session on an older bridge.
    """
    _host(monkeypatch)
    monkeypatch.setattr(identity, "seat_file_path",
                        lambda key=None: str(tmp_path / "absent.seat"))
    monkeypatch.setattr(identity, "_proc_info", lambda pid: None)
    assert identity.discover_session_process() is None


def test_a_failed_probe_is_not_a_death(monkeypatch):
    """"I could not ask" must never read as "the process is gone".

    The obvious spelling of this check — ``not process_is_alive(...)`` over
    ``_proc_info`` — has a hole: ``_proc_info`` returns None BOTH for a genuinely
    absent process AND when ``ps`` itself failed. Reading that None as "gone"
    turns a transient hiccup (timeout, fork pressure) into a death notice for a
    live session, which is the expensive direction and the same absent-is-not-
    dead conflation this project spent 2026-08-01 removing one layer up.
    """
    import subprocess

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=5)

    monkeypatch.setattr(subprocess, "run", _boom)
    assert identity.process_is_gone(1234, "whenever") is False

    class _Failed:
        stdout = ""
        returncode = 127  # ps missing / exec failure, NOT "no such process"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    assert identity.process_is_gone(1234, "whenever") is False


def test_a_definite_absence_is_a_death(monkeypatch):
    """The one answer that IS conclusive: ps ran and found no such process."""
    import subprocess

    class _Absent:
        stdout = ""
        returncode = 1  # POSIX ps: no matching process

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Absent())
    assert identity.process_is_gone(1234, "whenever") is True


# --- A discarded declaration must not be silent (2026-08-02) -------------


def test_env_override_of_a_declared_identity_is_announced(monkeypatch, tmp_path):
    """A committed .engram.cfg declaration that is overridden must SAY SO.

    The precedence itself is correct and load-bearing — a launcher has to seat
    each spawn distinctly, and .engram.cfg is per-folder so it cannot. What was
    wrong was the silence: a repo declared `beastchat-server`, ran as
    `beastchat-grok`, and nothing anywhere reported the divergence. Mail to the
    declared name reached nobody, and a peer began writing remediation for the
    wrong cause before anyone noticed.
    """
    identity._IDENTITY_OVERRIDE_NOTICE = None
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: "declared-name")
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "env-name")
    monkeypatch.setattr(identity, "_SESSION_SEAT", None)
    monkeypatch.setattr(identity, "read_seat_file", lambda: None)

    assert identity.resolve_session_identity("/x") == "env-name"  # env still wins
    notice = identity.identity_override_notice()
    assert notice is not None, "the discarded declaration was not reported"
    assert "declared-name" in notice and "env-name" in notice
    assert "reaches nobody" in notice


def test_no_notice_when_env_and_declaration_agree(monkeypatch):
    """Agreement is not a divergence — do not cry wolf on a matching pair."""
    identity._IDENTITY_OVERRIDE_NOTICE = None
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: "same-name")
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "same-name")
    monkeypatch.setattr(identity, "_SESSION_SEAT", None)
    monkeypatch.setattr(identity, "read_seat_file", lambda: None)

    assert identity.resolve_session_identity("/x") == "same-name"
    assert identity.identity_override_notice() is None


def test_no_notice_when_nothing_is_declared(monkeypatch):
    """An env-only session has overridden nothing — silence is correct there."""
    identity._IDENTITY_OVERRIDE_NOTICE = None
    monkeypatch.setattr(identity, "resolve_inbox_identity", lambda _d: None)
    monkeypatch.setenv(identity.INBOX_IDENTITY_ENV, "env-name")
    monkeypatch.setattr(identity, "_SESSION_SEAT", None)
    monkeypatch.setattr(identity, "read_seat_file", lambda: None)

    assert identity.resolve_session_identity("/x") == "env-name"
    assert identity.identity_override_notice() is None
