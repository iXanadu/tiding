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
