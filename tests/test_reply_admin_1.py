"""REPLY-ADMIN-1: a group convened by an admin session must record the
admin's host, or every participant's reply is refused (bare `admin` 409s)."""

from server.routers.memory import _participant_set


def test_admin_convener_keeps_its_host():
    got = _participant_set(["meidura", "engram"], sender="admin@hosta")
    assert got == ["meidura", "engram", "admin@hosta"]


def test_ordinary_sender_still_loosened():
    got = _participant_set(["meidura"], sender="engram-claude-6@hosta")
    assert "admin" not in got and "meidura" in got
