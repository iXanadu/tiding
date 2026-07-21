"""Client doctor — reach + operate memory, and the Host-400 diagnostic."""
import httpx
import respx
from unittest.mock import patch

from engram_mcp import doctor
from engram_mcp.config import settings as cfg


@respx.mock(base_url="http://localhost:8920")
async def test_healthy_client_all_pass(respx_mock):
    respx_mock.get("/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get("/whoami").mock(return_value=httpx.Response(200, json={
        "name": "dbone", "read_namespaces": ["fleet"]}))
    respx_mock.post("/memory/set").mock(return_value=httpx.Response(200, json={"status": "ok", "key": "k"}))
    respx_mock.post("/memory/search").mock(return_value=httpx.Response(200, json={
        "status": "ok", "results": [{"key": "k"}]}))
    respx_mock.post("/memory/forget").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with patch.object(cfg, "memory_api_token", "engram_test"):
        rc = await doctor.run()
    assert rc == 0


@respx.mock(base_url="http://localhost:8920")
async def test_host_rejected_400_gives_trusted_hosts_fix(respx_mock, capsys):
    """The marquee diagnostic: server 400 on /health => tell them to add this
    box to the server's ENGRAM_TRUSTED_HOSTS."""
    respx_mock.get("/health").mock(return_value=httpx.Response(400, text="Invalid host header"))
    with patch.object(cfg, "memory_api_token", "engram_test"):
        rc = await doctor.run()
    out = capsys.readouterr().out
    assert rc == 1
    assert "REJECTED this box's Host header" in out
    assert "ENGRAM_TRUSTED_HOSTS" in out


@respx.mock(base_url="http://localhost:8920")
async def test_unreachable_server_fails_clearly(respx_mock, capsys):
    respx_mock.get("/health").mock(side_effect=httpx.ConnectError("no route"))
    with patch.object(cfg, "memory_api_token", "engram_test"):
        rc = await doctor.run()
    out = capsys.readouterr().out
    assert rc == 1
    assert "Cannot reach the server" in out


@respx.mock(base_url="http://localhost:8920")
async def test_bad_token_401(respx_mock, capsys):
    respx_mock.get("/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get("/whoami").mock(return_value=httpx.Response(401, json={"detail": "invalid"}))
    with patch.object(cfg, "memory_api_token", "engram_bad"):
        rc = await doctor.run()
    out = capsys.readouterr().out
    assert rc == 1
    assert "Token rejected (401)" in out
