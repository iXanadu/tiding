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
