"""NAME-1 P2: TIDING_* env prefix accepted, ENGRAM_* fallback logged.

The shim lives in server/config.py and runs once at import; these tests call
the helper directly with a controlled env.
"""

import logging
import os

import server.config as config


def test_tiding_prefix_wins_over_engram(monkeypatch):
    monkeypatch.setenv("TIDING_DB_NAME", "tidingdb")
    monkeypatch.setenv("ENGRAM_DB_NAME", "engramdb")
    config._apply_env_prefix_compat()
    assert os.environ["ENGRAM_DB_NAME"] == "tidingdb"


def test_tiding_prefix_maps_when_legacy_absent(monkeypatch):
    monkeypatch.setenv("TIDING_EMBED_MODEL", "some/model")
    monkeypatch.delenv("ENGRAM_EMBED_MODEL", raising=False)
    config._apply_env_prefix_compat()
    assert os.environ["ENGRAM_EMBED_MODEL"] == "some/model"


def test_legacy_only_vars_logged_by_name(monkeypatch, caplog):
    monkeypatch.setenv("ENGRAM_TRUSTED_HOSTS", "localhost")
    monkeypatch.delenv("TIDING_TRUSTED_HOSTS", raising=False)
    with caplog.at_level(logging.INFO, logger="engram.config"):
        config._apply_env_prefix_compat()
    messages = [r.getMessage() for r in caplog.records]
    assert any("TIDING-PREFIX-COMPAT" in m for m in messages)
    assert any("ENGRAM_TRUSTED_HOSTS" in m for m in messages)


def test_var_with_new_twin_not_reported_legacy(monkeypatch, caplog):
    monkeypatch.setenv("TIDING_DB_NAME", "x")
    monkeypatch.setenv("ENGRAM_DB_NAME", "y")
    with caplog.at_level(logging.INFO, logger="engram.config"):
        config._apply_env_prefix_compat()
    for record in caplog.records:
        if "TIDING-PREFIX-COMPAT" in record.getMessage():
            assert "ENGRAM_DB_NAME" not in record.getMessage()
