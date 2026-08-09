"""Tests for model resolution — including that an unknown model stays unknown."""

import engram_mcp.model as model
from engram_mcp.model import (
    SOURCE_DECLARED,
    SOURCE_HARNESS_CONFIG,
    SOURCE_TRANSCRIPT,
    SOURCE_UNKNOWN,
    resolve_model,
)


def _clear_env(monkeypatch):
    for var in ("ENGRAM_MODEL", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    model._CACHE.clear()


def _transcript(tmp_path, monkeypatch, lines, sid="sess-1", pdir="/Users/x/projects/demo"):
    """Lay down a transcript where the resolver will look for it."""
    home = tmp_path / "home"
    slug = pdir.replace("/", "-")
    d = home / ".claude" / "projects" / slug
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(lines))
    monkeypatch.setattr(model.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", pdir)


def test_reads_model_from_this_sessions_transcript(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _transcript(tmp_path, monkeypatch, ['{"model":"claude-opus-5"}'])
    assert resolve_model("claude") == ("claude-opus-5", SOURCE_TRANSCRIPT)


def test_last_stamp_wins_when_the_model_changed_mid_session(tmp_path, monkeypatch):
    """The switch case is the whole point — 45 of 237 sessions did this."""
    _clear_env(monkeypatch)
    _transcript(
        tmp_path,
        monkeypatch,
        ['{"model":"claude-opus-5"}'] * 3 + ['{"model":"claude-fable-5"}'] * 2,
    )
    assert resolve_model("claude") == ("claude-fable-5", SOURCE_TRANSCRIPT)


def test_synthetic_is_not_a_model(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _transcript(
        tmp_path,
        monkeypatch,
        ['{"model":"claude-opus-5"}', '{"model":"<synthetic>"}'],
    )
    assert resolve_model("claude") == ("claude-opus-5", SOURCE_TRANSCRIPT)


def test_declared_env_wins_and_is_marked_as_declared(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _transcript(tmp_path, monkeypatch, ['{"model":"claude-opus-5"}'])
    monkeypatch.setenv("ENGRAM_MODEL", "grok-4.6")
    assert resolve_model("claude") == ("grok-4.6", SOURCE_DECLARED)


def test_declared_serves_a_harness_that_records_nothing(monkeypatch):
    """Cursor's only channel: it writes no model to disk."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ENGRAM_MODEL", "composer-2.5")
    assert resolve_model("cursor") == ("composer-2.5", SOURCE_DECLARED)


def test_unknown_provider_reports_unknown_never_a_default(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(model.Path, "home", staticmethod(lambda: tmp_path))
    assert resolve_model("grok") == (None, SOURCE_UNKNOWN)
    assert resolve_model("codex") == (None, SOURCE_UNKNOWN)
    # cursor with no config file to read is also unknown, not a guess
    assert resolve_model("cursor") == (None, SOURCE_UNKNOWN)


def _cursor_config(tmp_path, monkeypatch, payload):
    d = tmp_path / ".cursor"
    d.mkdir(parents=True, exist_ok=True)
    import json as _json
    (d / "cli-config.json").write_text(_json.dumps(payload))
    monkeypatch.setattr(model.Path, "home", staticmethod(lambda: tmp_path))


def test_cursor_model_comes_from_cli_config_marked_as_harness_config(tmp_path, monkeypatch):
    """Cursor records no per-session model; its global selection is the best available."""
    _clear_env(monkeypatch)
    _cursor_config(tmp_path, monkeypatch, {"model": {"modelId": "grok-4.5"}})
    assert resolve_model("cursor") == ("grok-4.5", SOURCE_HARNESS_CONFIG)


def test_declared_outranks_cursor_config(tmp_path, monkeypatch):
    """A driver setting the model per session knows better than a shared file."""
    _clear_env(monkeypatch)
    _cursor_config(tmp_path, monkeypatch, {"model": {"modelId": "grok-4.5"}})
    monkeypatch.setenv("ENGRAM_MODEL", "composer-2.5")
    assert resolve_model("cursor") == ("composer-2.5", SOURCE_DECLARED)


def test_malformed_cursor_config_is_unknown(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    d = tmp_path / ".cursor"; d.mkdir(parents=True)
    (d / "cli-config.json").write_text("{not json")
    monkeypatch.setattr(model.Path, "home", staticmethod(lambda: tmp_path))
    assert resolve_model("cursor") == (None, SOURCE_UNKNOWN)


def test_cursor_config_without_model_key_is_unknown(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _cursor_config(tmp_path, monkeypatch, {"version": 1})
    assert resolve_model("cursor") == (None, SOURCE_UNKNOWN)


def test_missing_session_env_is_unknown_not_a_guess(tmp_path, monkeypatch):
    """No session id means we cannot know WHICH transcript is ours.

    Guessing (newest file in the folder) would silently pick a co-worker's
    session, which is the co-working case this fleet runs constantly.
    """
    _clear_env(monkeypatch)
    _transcript(tmp_path, monkeypatch, ['{"model":"claude-opus-5"}'])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert resolve_model("claude") == (None, SOURCE_UNKNOWN)


def test_absent_transcript_is_unknown(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _transcript(tmp_path, monkeypatch, ['{"model":"claude-opus-5"}'], sid="sess-1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "no-such-session")
    assert resolve_model("claude") == (None, SOURCE_UNKNOWN)


def test_transcript_with_no_model_is_unknown(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    _transcript(tmp_path, monkeypatch, ['{"type":"user","text":"hi"}'])
    assert resolve_model("claude") == (None, SOURCE_UNKNOWN)


def test_reads_only_the_tail_of_a_large_transcript(tmp_path, monkeypatch):
    """Cost must stay flat as a transcript grows; old models must not resurface."""
    _clear_env(monkeypatch)
    filler = '{"pad":"' + "x" * 4000 + '"}'
    lines = ['{"model":"claude-opus-4-8"}'] + [filler] * 200 + ['{"model":"claude-opus-5"}']
    _transcript(tmp_path, monkeypatch, lines)
    got, src = resolve_model("claude")
    assert (got, src) == ("claude-opus-5", SOURCE_TRANSCRIPT)


def test_cache_invalidates_when_the_transcript_grows(tmp_path, monkeypatch):
    """A new turn must not be served a stale cached model."""
    _clear_env(monkeypatch)
    pdir = "/Users/x/projects/demo"
    _transcript(tmp_path, monkeypatch, ['{"model":"claude-opus-5"}'], pdir=pdir)
    assert resolve_model("claude")[0] == "claude-opus-5"
    path = tmp_path / "home" / ".claude" / "projects" / pdir.replace("/", "-") / "sess-1.jsonl"
    import os as _os

    st = path.stat()
    path.write_text('{"model":"claude-opus-5"}\n{"model":"claude-fable-5"}')
    _os.utime(path, (st.st_atime, st.st_mtime + 10))  # ensure mtime differs
    assert resolve_model("claude")[0] == "claude-fable-5"
