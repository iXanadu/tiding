"""TIME-1: server time stamped into every tool-result banner.

The client captures the HTTP Date header each response already carries;
the server module renders it as a `server time: <UTC ISO>` line.
"""

import httpx

import engram_mcp.client as client_mod
import engram_mcp.server as server_mod


def test_no_stamp_before_any_response(monkeypatch):
    monkeypatch.setattr(client_mod, "_LAST_SERVER_TIME", None)
    assert client_mod.last_server_time_iso() is None
    assert server_mod._server_time_line() == ""


def test_date_header_captured_and_rendered(monkeypatch):
    monkeypatch.setattr(client_mod, "_LAST_SERVER_TIME", None)
    resp = httpx.Response(200, headers={"date": "Tue, 19 Aug 2026 18:05:07 GMT"})
    client_mod._record_server_time(resp)
    assert client_mod.last_server_time_iso() == "2026-08-19T18:05:07Z"
    assert server_mod._server_time_line() == "server time: 2026-08-19T18:05:07Z\n"


def test_malformed_date_header_ignored(monkeypatch):
    monkeypatch.setattr(client_mod, "_LAST_SERVER_TIME", None)
    client_mod._record_server_time(httpx.Response(200, headers={"date": "not-a-date"}))
    assert client_mod.last_server_time_iso() is None


def test_missing_date_header_keeps_previous(monkeypatch):
    monkeypatch.setattr(client_mod, "_LAST_SERVER_TIME", None)
    client_mod._record_server_time(
        httpx.Response(200, headers={"date": "Tue, 19 Aug 2026 18:05:07 GMT"})
    )
    client_mod._record_server_time(httpx.Response(200))
    assert client_mod.last_server_time_iso() == "2026-08-19T18:05:07Z"
