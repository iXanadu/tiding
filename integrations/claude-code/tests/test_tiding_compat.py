"""NAME-1 P2 compat in the bridge: TIDING_* env family, .tiding.cfg read
preference, ~/.config/tiding identity-file preference (per-file, so a
half-migrated box never silently loses its token).
"""

import os

import engram_mcp.config as config
import engram_mcp.scoping as scoping


def test_env_prefix_maps_when_legacy_absent(monkeypatch):
    monkeypatch.setenv("TIDING_PROVIDER", "grok")
    monkeypatch.delenv("ENGRAM_PROVIDER", raising=False)
    config._apply_env_prefix_compat()
    assert os.environ["ENGRAM_PROVIDER"] == "grok"


def test_env_prefix_tiding_wins(monkeypatch):
    monkeypatch.setenv("TIDING_PROVIDER", "grok")
    monkeypatch.setenv("ENGRAM_PROVIDER", "claude")
    config._apply_env_prefix_compat()
    assert os.environ["ENGRAM_PROVIDER"] == "grok"


def test_cfg_walkup_prefers_tiding(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = oldname\n")
    (tmp_path / ".tiding.cfg").write_text("project = newname\n")
    assert scoping.resolve_project_name(str(tmp_path)) == "newname"


def test_cfg_walkup_engram_fallback(tmp_path):
    (tmp_path / ".engram.cfg").write_text("project = oldname\n")
    assert scoping.resolve_project_name(str(tmp_path)) == "oldname"


def test_writes_still_produce_engram_cfg(tmp_path):
    # Deployed pre-P2 bridges can only read .engram.cfg — writes stay on the
    # legacy name until the fleet's readers have all upgraded (WIRE-1 class).
    path = scoping.write_project_cfg(str(tmp_path), "someproj")
    assert path.endswith(".engram.cfg")
    assert scoping.resolve_project_name(str(tmp_path)) == "someproj"


def _identity_dirs(tmp_path, monkeypatch):
    tiding = tmp_path / "tiding"
    engram = tmp_path / "engram"
    tiding.mkdir()
    engram.mkdir()
    monkeypatch.setattr(config, "TIDING_IDENTITY_FILE", str(tiding / "identity"))
    monkeypatch.setattr(config, "IDENTITY_FILE", str(engram / "identity"))
    monkeypatch.setattr(config, "TIDING_IDENTITIES_DIR", str(tiding / "identities"))
    monkeypatch.setattr(config, "IDENTITIES_DIR", str(engram / "identities"))
    return tiding, engram


def test_identity_file_prefers_tiding(tmp_path, monkeypatch):
    tiding, engram = _identity_dirs(tmp_path, monkeypatch)
    (tiding / "identity").write_text("memory_api_token=new\n")
    (engram / "identity").write_text("memory_api_token=old\n")
    monkeypatch.delenv("ENGRAM_IDENTITY", raising=False)
    path, _ = config._resolve_identity_file()
    assert path == str(tiding / "identity")


def test_identity_file_engram_fallback(tmp_path, monkeypatch):
    tiding, engram = _identity_dirs(tmp_path, monkeypatch)
    (engram / "identity").write_text("memory_api_token=old\n")
    monkeypatch.delenv("ENGRAM_IDENTITY", raising=False)
    path, _ = config._resolve_identity_file()
    assert path == str(engram / "identity")


def test_identity_selector_prefers_tiding_then_falls_back(tmp_path, monkeypatch):
    tiding, engram = _identity_dirs(tmp_path, monkeypatch)
    (tiding / "identities").mkdir()
    (engram / "identities").mkdir()
    (engram / "identities" / "worker").write_text("memory_api_token=old\n")
    monkeypatch.setenv("ENGRAM_IDENTITY", "worker")
    path, _ = config._resolve_identity_file()
    assert path == str(engram / "identities" / "worker")
    (tiding / "identities" / "worker").write_text("memory_api_token=new\n")
    path, _ = config._resolve_identity_file()
    assert path == str(tiding / "identities" / "worker")
