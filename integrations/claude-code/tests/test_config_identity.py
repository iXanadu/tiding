"""Identity-store resolution (docs/design/provider-credentials.md).

Covers _resolve_identity_file: the ENGRAM_IDENTITY selector, the loud error
on a dangling selector, the legacy-file fallback, and the bare-defaults case.
"""

import pytest

import engram_mcp.config as config
from engram_mcp.config import IdentitySelectorError, _resolve_identity_file


@pytest.fixture
def fake_store(tmp_path, monkeypatch):
    """Point the module's path constants at a temp identity store."""
    identities = tmp_path / "identities"
    identities.mkdir()
    legacy = tmp_path / "identity"
    monkeypatch.setattr(config, "IDENTITIES_DIR", str(identities))
    monkeypatch.setattr(config, "IDENTITY_FILE", str(legacy))
    monkeypatch.delenv("ENGRAM_IDENTITY", raising=False)
    return identities, legacy


def test_selector_picks_named_identity(fake_store, monkeypatch):
    identities, _ = fake_store
    (identities / "grok").write_text("memory_api_token=engram_grok_test\n")
    monkeypatch.setenv("ENGRAM_IDENTITY", "grok")
    path, source = _resolve_identity_file()
    assert path == str(identities / "grok")
    assert "identity 'grok'" in source


def test_dangling_selector_is_a_loud_error_not_a_fallback(fake_store, monkeypatch):
    """A bad selector must never silently impersonate another identity."""
    _, legacy = fake_store
    legacy.write_text("memory_api_token=engram_claude_test\n")  # tempting fallback
    monkeypatch.setenv("ENGRAM_IDENTITY", "gpt")  # no such file
    with pytest.raises(IdentitySelectorError, match="gpt"):
        _resolve_identity_file()


def test_no_selector_falls_back_to_legacy_file(fake_store):
    _, legacy = fake_store
    legacy.write_text("memory_api_token=engram_claude_test\n")
    path, source = _resolve_identity_file()
    assert path == str(legacy)
    assert "legacy identity file" in source


def test_nothing_configured_resolves_to_defaults(fake_store):
    path, source = _resolve_identity_file()
    assert path is None
    assert "defaults" in source


def test_whitespace_selector_treated_as_unset(fake_store, monkeypatch):
    _, legacy = fake_store
    legacy.write_text("memory_api_token=engram_claude_test\n")
    monkeypatch.setenv("ENGRAM_IDENTITY", "   ")
    path, _ = _resolve_identity_file()
    assert path == str(legacy)
