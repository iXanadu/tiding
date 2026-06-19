"""Tests for inbox identity computation — including the per-session override."""

import engram_mcp.identity as identity
from engram_mcp.identity import compute_identity


def _host(monkeypatch):
    monkeypatch.setattr(identity, "hostname", lambda: "macmini")


def test_default_identity_is_project_derived(monkeypatch):
    _host(monkeypatch)
    monkeypatch.delenv(identity.INBOX_IDENTITY_ENV, raising=False)
    monkeypatch.setattr(identity, "derive_project_name", lambda _d: "beastchat")
    reader, listen_set = compute_identity("/whatever")
    assert reader == "beastchat@macmini"
    assert listen_set == ["beastchat", "machine:macmini", "beastchat@macmini"]


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
