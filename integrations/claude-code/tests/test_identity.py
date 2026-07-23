"""Tests for inbox identity computation — including the per-session override."""

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
    assert listen_set == [
        "beastchat-app",
        "beastchat",
        "machine:macmini",
        "beastchat-app@macmini",
    ]


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


def test_explicit_project_dir_overrides_startup_cwd(monkeypatch):
    """An explicit arg still wins over the cwd anchor (admin cross-project work,
    or a session operating on a dir other than its cwd)."""
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
            "meidura-audit", "meidura", "machine:macmini", "meidura-audit@macmini",
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
