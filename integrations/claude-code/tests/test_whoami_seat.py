"""SEAT-SELF-LOOKUP (2026-08-21): memory_whoami answers the question its name
promises — the INBOX IDENTITY the register actually granted this session (an
ordinal when the asked-for name was held) and the addresses it listens on.

Before: whoami printed only the token principal ("claude-code"), which every
session on the box shares, so a grok seat asking "who am I" could not learn
its own address from the tool named for it (it had to read memory_inbox's
footer or a wake-stream parenthetical). A peer hit /session/seats raw with no
bearer (401) looking for the same fact.
"""

import pytest

from engram_mcp import server as srv


@pytest.mark.asyncio
async def test_whoami_prints_granted_seat_and_listen_set(monkeypatch):
    async def _who():
        return {"name": "claude-code", "type": "agent", "is_admin": False,
                "read_namespaces": ["fleet"], "write_namespaces": ["fleet"]}

    async def _ns():
        return {"read": ["fleet"], "write": ["fleet"]}

    def _ident(project_dir):
        assert project_dir == "/tmp/proj"
        return ("proj-grok-2@macmini",
                ["proj-grok-2", "proj-grok", "proj", "machine:macmini"])

    monkeypatch.setattr(srv.settings, "memory_api_token", "engram_x")
    monkeypatch.setattr(srv._client, "whoami", _who)
    monkeypatch.setattr(srv._client, "namespaces", _ns)
    monkeypatch.setattr(srv, "compute_identity", _ident)

    out = await srv.memory_whoami(project_dir="/tmp/proj")
    assert "Principal: claude-code" in out
    assert "Inbox identity (your granted seat): proj-grok-2@macmini" in out
    assert "Listening on: ['proj-grok-2', 'proj-grok', 'proj', 'machine:macmini']" in out


@pytest.mark.asyncio
async def test_whoami_still_answers_when_seat_resolution_fails(monkeypatch):
    async def _who():
        return {"name": "claude-code", "type": "agent", "is_admin": False,
                "read_namespaces": ["fleet"], "write_namespaces": ["fleet"]}

    async def _ns():
        return {"read": ["fleet"], "write": ["fleet"]}

    def _boom(project_dir):
        raise RuntimeError("no seat file")

    monkeypatch.setattr(srv.settings, "memory_api_token", "engram_x")
    monkeypatch.setattr(srv._client, "whoami", _who)
    monkeypatch.setattr(srv._client, "namespaces", _ns)
    monkeypatch.setattr(srv, "compute_identity", _boom)

    out = await srv.memory_whoami()
    assert "Principal: claude-code" in out
    assert "Inbox identity: (could not resolve" in out
