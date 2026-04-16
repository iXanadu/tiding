import socket
from unittest.mock import patch

from engram_mcp.scoping import (
    _parse_engram_cfg,
    resolve_project_name,
    resolve_scope_and_user_id,
)


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


def test_home_cfg_used_from_home_adjacent_dir(tmp_path, monkeypatch):
    # ~/Downloads or similar (outside ~/projects) walks up and picks up ~/.engram.cfg.
    _fake_home(tmp_path, monkeypatch)
    (tmp_path / ".engram.cfg").write_text("project = admin\n")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    assert resolve_project_name(str(downloads)) == "admin"


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
