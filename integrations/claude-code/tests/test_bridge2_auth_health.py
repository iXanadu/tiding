"""BRIDGE-2: a dead credential must not be invisible to the person holding it.

Measured 2026-08-16 on the operator's own desktop: a rotated token sat in an
app config for weeks-to-months. The bridge retried claim/presence on a timer,
non-fatal by design, hammering prod with 401s every ~2min, forever, silently.
The operator learned of it from a peer agent reading server logs.

Two halves, both tested here: it must SAY SO (naming the config source, so the
fix is one step and not a hunt), and it must STOP (a hard refusal is a final
answer, not a blip).
"""

import engram_mcp.client as client
import engram_mcp.server as server


def setup_function():
    client._AUTH_FAILURES = 0
    client._AUTH_LAST_STATUS = None
    client._AUTH_LAST_PATH = None
    server._AUTH_BANNER_SHOWN = False


def test_a_single_refusal_is_not_yet_a_verdict():
    """One 401 can be a token rotated in-flight between two calls. Crying wolf
    on the first is how a real alarm gets trained away."""
    client._note_auth_failure(401, "/memory/search")
    assert not client.auth_is_refused()
    assert server._auth_health_banner() == ""


def test_repeated_refusal_names_the_config_source_and_stops_retrying():
    for _ in range(client.AUTH_REFUSAL_LIMIT):
        client._note_auth_failure(401, "/session/presence")

    assert client.auth_is_refused(), "best-effort retries must give up"

    banner = server._auth_health_banner()
    assert "CREDENTIAL REFUSED" in banner
    assert "401" in banner
    # The whole point: name WHERE the bad token came from.
    assert str(server.CONFIG_SOURCE) in banner
    # And say the retries stopped, so silence is not read as health.
    assert "STOPPED" in banner


def test_banner_shows_once_not_on_every_call():
    for _ in range(client.AUTH_REFUSAL_LIMIT):
        client._note_auth_failure(403, "/memory/store")
    assert server._auth_health_banner() != ""
    assert server._auth_health_banner() == "", "a banner on every result is noise"


def test_one_authorised_call_clears_the_streak():
    """A token fixed at the source must recover without a restart — otherwise
    the cure requires knowing to restart, which is its own hidden step."""
    for _ in range(client.AUTH_REFUSAL_LIMIT):
        client._note_auth_failure(401, "/memory/search")
    assert client.auth_is_refused()

    client._note_auth_ok()

    assert not client.auth_is_refused()
    assert client.auth_health()[0] == 0


# The tests above assert the PREDICATE. That is not the claim. An earlier
# draft of this file stayed green while the stop-gate was neutered at its call
# site, because nothing here exercised _heartbeat itself — the same "verified
# the mechanism, never the claim" failure this whole class is about. So:

import pytest


@pytest.mark.asyncio
async def test_heartbeat_actually_stops_calling_the_server_when_refused(monkeypatch):
    """DESTINATION CHECK: not 'is the flag set' — did the REQUEST stop."""
    calls: list[str] = []

    async def _spy(**kwargs):
        calls.append("presence")
        return {}

    monkeypatch.setattr(server._client, "presence_update", _spy)
    monkeypatch.setattr(server, "_claim_seat", lambda *a, **k: _noop())
    server._last_heartbeat = 0.0

    # healthy: the heartbeat speaks
    await server._heartbeat(None)
    assert calls == ["presence"], "precondition: a healthy heartbeat must call out"

    # refused: it must go quiet
    for _ in range(client.AUTH_REFUSAL_LIMIT):
        client._note_auth_failure(401, "/session/presence")
    server._last_heartbeat = 0.0
    await server._heartbeat(None)

    assert calls == ["presence"], (
        "BRIDGE-2 FAIL: the heartbeat kept calling a server that has already "
        "refused this credential — this is the weeks of silent 401s."
    )


async def _noop():
    return None


def test_the_banner_actually_reaches_a_tool_result():
    """Same discipline: a banner function nobody wired in is a fix that exists
    only in prose. Assert it lands on the channel a reader actually sees."""
    for _ in range(client.AUTH_REFUSAL_LIMIT):
        client._note_auth_failure(401, "/memory/search")

    rendered = server._append_guidance("some tool output", {})

    assert "CREDENTIAL REFUSED" in rendered, (
        "the banner exists but never reaches a tool result — the reader sees "
        "nothing, which is the defect, not the fix"
    )
    assert "some tool output" in rendered
