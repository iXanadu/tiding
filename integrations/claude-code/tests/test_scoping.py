import socket
from unittest.mock import patch

import pytest

from engram_mcp.scoping import (
    AmbiguousIdentity,
    _parse_engram_cfg,
    ensure_project_identity,
    resolve_inbox_identity,
    resolve_partition,
    resolve_project_name,
    resolve_scope_and_user_id,
    write_project_cfg,
)


def test_resolve_inbox_identity_from_cfg(tmp_path):
    (tmp_path / ".engram.cfg").write_text(
        "project = beastchat\ninbox_identity = beastchat-server\n"
    )
    # project and inbox identity come from the SAME file but are independent
    assert resolve_project_name(str(tmp_path)) == "beastchat"
    assert resolve_inbox_identity(str(tmp_path)) == "beastchat-server"


def test_resolve_inbox_identity_absent_is_none(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = beastchat\n")
    assert resolve_project_name(str(tmp_path)) == "beastchat"
    assert resolve_inbox_identity(str(tmp_path)) is None


def test_resolve_inbox_identity_walks_up(tmp_path):
    (tmp_path / ".engram.cfg").write_text(
        "project = beastchat\ninbox_identity = beastchat-app\n"
    )
    sub = tmp_path / "ios" / "src"
    sub.mkdir(parents=True)
    assert resolve_inbox_identity(str(sub)) == "beastchat-app"


def test_parse_engram_cfg_reads_named_key(tmp_path):
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text("project = foo\ninbox_identity = foo-app\n")
    assert _parse_engram_cfg(str(cfg), "inbox_identity") == "foo-app"
    assert _parse_engram_cfg(str(cfg)) == "foo"  # defaults to project


def test_machine_scope():
    hostname = socket.gethostname().split(".")[0].lower()
    assert resolve_scope_and_user_id("machine") == ("machine", hostname)


def test_machine_scope_is_default():
    result = resolve_scope_and_user_id(None, default_scope="machine")
    hostname = socket.gethostname().split(".")[0].lower()
    assert result == ("machine", hostname)


def test_shared_scope():
    assert resolve_scope_and_user_id("shared") == ("shared", "global")


def test_project_scope_with_project_dir():
    assert resolve_scope_and_user_id("project", project_dir="/Users/test/projects/my-app") == ("project", "my-app")


def test_project_scope_with_basename_only():
    assert resolve_scope_and_user_id("project", project_dir="my-app") == ("project", "my-app")


def test_project_scope_falls_back_to_cwd():
    with patch("engram_mcp.scoping.os.getcwd", return_value="/Users/test/projects/fallback-app"):
        assert resolve_scope_and_user_id("project") == ("project", "fallback-app")


def test_custom_scope_passthrough():
    assert resolve_scope_and_user_id("custom-thing") == ("custom-thing", "custom-thing")


def test_none_scope_uses_default():
    assert resolve_scope_and_user_id(None, default_scope="shared") == ("shared", "global")


# --- .engram.cfg resolution ---

def test_parse_engram_cfg_simple(tmp_path):
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text("project = newTag\n")
    assert _parse_engram_cfg(str(cfg)) == "newTag"


def test_parse_engram_cfg_tolerates_whitespace_and_comments(tmp_path):
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text("# engram identifier\n\n  project   =   newTag  \n")
    assert _parse_engram_cfg(str(cfg)) == "newTag"


def test_parse_engram_cfg_strips_quotes(tmp_path):
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text('project = "newTag"\n')
    assert _parse_engram_cfg(str(cfg)) == "newTag"


def test_parse_engram_cfg_rejects_invalid_name(tmp_path):
    # Path separators in the value are refused — prevents user_id injection.
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text("project = ../evil\n")
    assert _parse_engram_cfg(str(cfg)) is None


def test_parse_engram_cfg_missing_key(tmp_path):
    cfg = tmp_path / ".engram.cfg"
    cfg.write_text("name = newTag\n")
    assert _parse_engram_cfg(str(cfg)) is None


def test_parse_engram_cfg_nonexistent():
    assert _parse_engram_cfg("/nonexistent/.engram.cfg") is None


def test_resolve_project_name_from_project_root(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = newTag\n")
    assert resolve_project_name(str(tmp_path)) == "newTag"


def test_resolve_project_name_walks_up_from_subdir(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = newTag\n")
    sub = tmp_path / "server" / "routers"
    sub.mkdir(parents=True)
    assert resolve_project_name(str(sub)) == "newTag"


def test_resolve_project_name_prod_layout(tmp_path):
    # The core case: /var/www/site/prod — basename alone would collapse to 'prod'.
    site = tmp_path / "trustworthyagents.com"
    prod = site / "prod"
    prod.mkdir(parents=True)
    (site / ".engram.cfg").write_text("project = newTag\n")
    assert resolve_project_name(str(prod)) == "newTag"


def test_resolve_project_name_no_cfg(tmp_path):
    assert resolve_project_name(str(tmp_path)) is None


def test_resolve_project_name_bare_basename_returns_none():
    # Non-absolute input — we can't walk it, so skip the cfg lookup and let
    # the caller fall back to basename.
    assert resolve_project_name("my-app") is None


def test_resolve_project_name_none():
    assert resolve_project_name(None) is None


def test_project_scope_uses_engram_cfg_over_basename(tmp_path):
    # Critical: prod-layout path resolves to declared name, not 'prod'.
    site = tmp_path / "trustworthyagents.com"
    prod = site / "prod"
    prod.mkdir(parents=True)
    (site / ".engram.cfg").write_text("project = newTag\n")
    assert resolve_scope_and_user_id("project", project_dir=str(prod)) == ("project", "newTag")


def test_project_scope_falls_back_when_cfg_malformed(tmp_path):
    (tmp_path / ".engram.cfg").write_text("garbage\n")
    # Falls through to basename fallback.
    assert resolve_scope_and_user_id("project", project_dir=str(tmp_path)) == ("project", tmp_path.name)


# --- $HOME/projects boundary behavior ---
#
# When project_dir is under $HOME/projects/, walk-up must stop at $HOME/projects
# and NOT cross into $HOME. This prevents child projects without their own
# .engram.cfg from inheriting $HOME/.engram.cfg (which declares admin-session
# identity, not a default for projects).

def _fake_home(tmp_path, monkeypatch):
    """Point os.path.expanduser('~') at tmp_path so we can simulate $HOME."""
    fake_home = str(tmp_path)
    monkeypatch.setattr("engram_mcp.scoping.os.path.expanduser", lambda p: fake_home if p == "~" else p)
    projects = tmp_path / "projects"
    projects.mkdir()
    return fake_home, projects


def test_home_cfg_not_inherited_by_child_project(tmp_path, monkeypatch):
    # ~/.engram.cfg exists (admin identity). Child project under ~/projects
    # without its own cfg must NOT pick up admin — it falls back to basename.
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    child = tmp_path / "projects" / "foo"
    child.mkdir()
    assert resolve_project_name(str(child)) is None


def test_home_cfg_not_inherited_by_nested_child(tmp_path, monkeypatch):
    # Walk-up from deeply nested project without cfg still stops at projects boundary.
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    nested = tmp_path / "projects" / "evh" / "evh"
    nested.mkdir(parents=True)
    assert resolve_project_name(str(nested)) is None


def test_child_project_cfg_still_wins(tmp_path, monkeypatch):
    # Child with its own cfg beats ~/.engram.cfg (which would be ignored anyway).
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    child = tmp_path / "projects" / "foo"
    child.mkdir()
    (child / ".engram.cfg").write_text("project = foo\n")
    assert resolve_project_name(str(child)) == "foo"


def test_home_cfg_used_when_at_home_directly(tmp_path, monkeypatch):
    # Admin session runs at $HOME itself: ~/.engram.cfg is read.
    fake_home, _ = _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    assert resolve_project_name(fake_home) == "admin"


def test_home_adjacent_dir_does_NOT_inherit_home_cfg(tmp_path, monkeypatch):
    # ~/Downloads does NOT silently inherit ~/.engram.cfg. Walk-up stops at
    # $HOME boundary — $HOME/.engram.cfg is only read when CWD IS $HOME.
    # Rule 3 (ambiguous) applies for home-adjacent dirs.
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    assert resolve_project_name(str(downloads)) is None


# --- ensure_project_identity: auto-write rules + ambiguous raise ---

def test_ensure_identity_rule1_at_home_autowrites_admin(tmp_path, monkeypatch):
    # Rule 1: CWD == $HOME → auto-write $HOME/.engram.cfg with project=admin.
    fake_home, _ = _fake_home(tmp_path, monkeypatch)
    assert not (tmp_path / ".engram.cfg").exists()
    name = ensure_project_identity(fake_home)
    assert name == "admin"
    cfg = tmp_path / ".engram.cfg"
    assert cfg.exists()
    assert "project = admin" in cfg.read_text()


def test_ensure_identity_rule1_idempotent(tmp_path, monkeypatch):
    # Rule 1 doesn't re-write if cfg already exists.
    fake_home, _ = _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n# manual\n")
    name = ensure_project_identity(fake_home)
    assert name == "admin"
    # Marker preserved — we didn't overwrite.
    assert "# manual" in (tmp_path / ".engram.cfg").read_text()


def test_ensure_identity_clean_layout_interrogates_not_autowrites(tmp_path, monkeypatch):
    # Option A (Decision 2026-07-18): a clean ~/projects/<x>/.claude/ layout with no
    # .engram.cfg must NOT silently auto-adopt the basename — it raises so the
    # user confirms. The basename is offered as the suggestion, cfg NOT written.
    _fake_home(tmp_path, monkeypatch)
    project = tmp_path / "projects" / "foo"
    project.mkdir(parents=True)
    (project / ".claude").mkdir()
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(project))
    assert exc_info.value.suggested == "foo"
    assert not (project / ".engram.cfg").exists()  # nothing written without consent


def test_ensure_identity_clean_layout_from_subdir_interrogates(tmp_path, monkeypatch):
    # Same, walking up from a subdir — still interrogates, still writes nothing.
    _fake_home(tmp_path, monkeypatch)
    project = tmp_path / "projects" / "foo"
    project.mkdir(parents=True)
    (project / ".claude").mkdir()
    subdir = project / "server" / "routers"
    subdir.mkdir(parents=True)
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(subdir))
    assert exc_info.value.suggested == "routers"  # basename of the queried dir
    assert not (project / ".engram.cfg").exists()


def test_ensure_identity_existing_real_cfg_wins(tmp_path, monkeypatch):
    # A hand-written / previously-declared .engram.cfg with a REAL name wins,
    # no interrogation (honors "sometimes I edit .engram first").
    _fake_home(tmp_path, monkeypatch)
    project = tmp_path / "projects" / "foo"
    project.mkdir(parents=True)
    (project / ".claude").mkdir()
    (project / ".engram.cfg").write_text("project = custom-name\n")
    name = ensure_project_identity(str(project))
    assert name == "custom-name"


def test_ensure_identity_sentinel_cfg_is_treated_as_unset(tmp_path, monkeypatch):
    # A .engram.cfg carrying a deploy label / placeholder is NOT a real
    # identity — interrogate rather than adopt it.
    _fake_home(tmp_path, monkeypatch)
    project = tmp_path / "projects" / "site"
    project.mkdir(parents=True)
    (project / ".engram.cfg").write_text("project = prod\n")
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(project))
    # 'site' is a real basename → offered as the suggestion
    assert exc_info.value.suggested == "site"


def test_ensure_identity_sentinel_basename_not_suggested(tmp_path, monkeypatch):
    # When the folder name itself is a deploy label, don't propose it — the
    # prompt asks for a real name (suggested == "").
    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "projects" / "prod"
    target.mkdir(parents=True)
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(target))
    assert exc_info.value.suggested == ""


def test_resolve_project_name_rejects_sentinel_value(tmp_path):
    # resolve_project_name treats a sentinel cfg value as unset (None).
    (tmp_path / ".engram.cfg").write_text("project = staging\n")
    assert resolve_project_name(str(tmp_path)) is None


def test_resolve_project_name_admin_is_real(tmp_path):
    # 'admin' is a real, intentional identity — NOT a sentinel.
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    assert resolve_project_name(str(tmp_path)) == "admin"


def test_ensure_identity_rule3_raises_for_home_adjacent(tmp_path, monkeypatch):
    # Rule 3: ~/Documents/HomeMaintenance/ has no cfg, no ~/projects path,
    # no .claude/. Must raise AmbiguousIdentity with suggested basename.
    fake_home, _ = _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "Documents" / "HomeMaintenance"
    target.mkdir(parents=True)
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(target))
    assert exc_info.value.suggested == "HomeMaintenance"
    assert exc_info.value.project_dir == str(target)


def test_ensure_identity_rule3_raises_for_projects_without_claude(tmp_path, monkeypatch):
    # ~/projects/foo/ WITHOUT .claude/ is not a project by our signal.
    # Rule 3 fires — ask the user.
    _fake_home(tmp_path, monkeypatch)
    project = tmp_path / "projects" / "foo"
    project.mkdir(parents=True)
    # no .claude/ directory
    with pytest.raises(AmbiguousIdentity) as exc_info:
        ensure_project_identity(str(project))
    assert exc_info.value.suggested == "foo"


def test_ensure_identity_rule3_raises_for_tmp(tmp_path, monkeypatch):
    # /tmp/scratch (outside $HOME entirely) with nothing → Rule 3.
    _fake_home(tmp_path, monkeypatch)
    target = tmp_path.parent / "scratch-ambiguous-test"
    target.mkdir(exist_ok=True)
    try:
        with pytest.raises(AmbiguousIdentity) as exc_info:
            ensure_project_identity(str(target))
        assert exc_info.value.suggested == "scratch-ambiguous-test"
    finally:
        target.rmdir()


def test_write_project_cfg_validates_name(tmp_path):
    with pytest.raises(ValueError):
        write_project_cfg(str(tmp_path), "../evil")
    with pytest.raises(ValueError):
        write_project_cfg(str(tmp_path), "has spaces")


def test_resolve_scope_keeps_basename_fallback_for_library_callers(tmp_path, monkeypatch):
    # resolve_scope_and_user_id (library function) keeps backward-compatible
    # basename fallback — doesn't raise. Only ensure_project_identity raises.
    # MCP tool layer wraps both to auto-write + prompt.
    _fake_home(tmp_path, monkeypatch)
    target = tmp_path / "Documents" / "X"
    target.mkdir(parents=True)
    assert resolve_scope_and_user_id("project", project_dir=str(target)) == ("project", "X")


# --- resolve_partition (Phase 4: project as first-class field) ---

def test_partition_machine():
    scope, user_id, project = resolve_partition("machine")
    assert scope == "machine"
    assert user_id == socket.gethostname().split(".")[0].lower()
    assert project is None


def test_partition_shared():
    assert resolve_partition("shared") == ("shared", "global", None)


def test_partition_project_with_principal(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = engram\n")
    scope, user_id, project = resolve_partition(
        "project", project_dir=str(tmp_path), principal_name="ixanadu"
    )
    assert scope == "project"
    assert user_id == "ixanadu"
    assert project == "engram"


def test_partition_project_without_principal_uses_unknown(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = engram\n")
    scope, user_id, project = resolve_partition("project", project_dir=str(tmp_path))
    assert (scope, user_id, project) == ("project", "unknown", "engram")


def test_partition_project_falls_back_to_basename(tmp_path):
    # No .engram.cfg → basename of project_dir
    scope, user_id, project = resolve_partition(
        "project", project_dir=str(tmp_path), principal_name="ixanadu"
    )
    assert project == tmp_path.name


def test_partition_custom_scope_passthrough():
    assert resolve_partition("user") == ("user", "user", None)


def test_server_path_unaffected_by_home_boundary(tmp_path, monkeypatch):
    # /opt/srv/engram (or anywhere outside ~/projects) walks up normally.
    # Uses a separate tmp tree to represent the server path.
    _fake_home(tmp_path, monkeypatch)
    # Build a fake server deploy: <tmp>/srv/engram/.engram.cfg
    server = tmp_path.parent / "srv_test" / "engram"
    server.mkdir(parents=True, exist_ok=True)
    (server / ".engram.cfg").write_text("project = engram\n")
    try:
        assert resolve_project_name(str(server)) == "engram"
    finally:
        (server / ".engram.cfg").unlink(missing_ok=True)
        server.rmdir()
        server.parent.rmdir()
