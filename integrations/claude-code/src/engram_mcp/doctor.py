"""engram client doctor — can THIS box reach and operate memory?

    python -m engram_mcp.doctor      (or the `engram-doctor` console script)

The client-side counterpart to the server's `python -m server.preflight`. A
client (bridge/watcher) box doesn't run the server — its job is to operate
memory against a remote one. This checks exactly that: config resolves, the
server is reachable, the token authenticates, and a real store/search/forget
roundtrip succeeds. Each finding prints an exact fix. Exits non-zero on FAIL.

The sharp one: if the server rejects this box's Host header (HTTP 400), it
tells you to add this box's reach-name to the SERVER's ENGRAM_TRUSTED_HOSTS —
diagnosing the Tailscale/LAN reach class from the client end.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from engram_mcp.config import CONFIG_SOURCE, settings

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_PROBE_KEY = "probe/doctor-roundtrip"


def _line(level, msg, fix=""):
    return (level, msg, fix)


async def run() -> int:
    url = settings.memory_api_url.rstrip("/")
    token = settings.memory_api_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    results = []

    # 1. config resolves
    if token:
        results.append(_line(PASS, f"Config: token loaded from {CONFIG_SOURCE}; server = {url}."))
    else:
        results.append(_line(WARN, f"Config: no token ({CONFIG_SOURCE}); server = {url}.",
                             "If the server requires auth, set memory_api_token in the "
                             "identity file (or ENGRAM_IDENTITY selector)."))

    reachable = authed = False
    async with httpx.AsyncClient(timeout=8.0) as c:
        # 2. server reachable
        try:
            r = await c.get(f"{url}/health")
            if r.status_code == 200 and "ok" in r.text:
                reachable = True
                results.append(_line(PASS, f"Server reachable at {url} (health ok)."))
            elif r.status_code == 400:
                results.append(_line(FAIL,
                    f"Server returned 400 to /health — it REJECTED this box's Host header.",
                    f"Add this box's reach-name to the SERVER's ENGRAM_TRUSTED_HOSTS "
                    f"(the host in {url}), then restart the server."))
            else:
                results.append(_line(FAIL, f"Server at {url} answered /health with HTTP {r.status_code}.",
                                     "Check the URL and that the server is healthy."))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            results.append(_line(FAIL, f"Cannot reach the server at {url} ({type(e).__name__}).",
                f"Check memory_api_url, that the server is running, and that this box "
                f"can route to it (Tailscale/LAN/firewall). This is the #1 client failure."))

        # 3. authenticate
        if reachable:
            try:
                r = await c.get(f"{url}/whoami", headers=headers)
                if r.status_code == 200:
                    authed = True
                    who = r.json()
                    results.append(_line(PASS,
                        f"Authenticated as principal '{who.get('name')}' "
                        f"(read: {','.join(who.get('read_namespaces', [])) or '—'})."))
                elif r.status_code == 401:
                    results.append(_line(FAIL, "Token rejected (401) — invalid or inactive.",
                        "Fix the token in the identity file (mint/regenerate on the server)."))
                elif r.status_code == 400:
                    results.append(_line(FAIL, "Server rejected this box's Host header (400).",
                        f"Add this box to the server's ENGRAM_TRUSTED_HOSTS."))
                else:
                    results.append(_line(FAIL, f"/whoami returned HTTP {r.status_code}."))
            except Exception as e:  # noqa: BLE001
                results.append(_line(FAIL, f"/whoami failed ({type(e).__name__})."))

        # 4. real memory roundtrip (the thing that actually matters)
        if authed:
            ns = settings.memory_namespace
            uid = "doctor-probe"
            try:
                s = await c.post(f"{url}/memory/set", headers=headers, json={
                    "namespace": ns, "key": _PROBE_KEY, "value": "engram client doctor roundtrip",
                    "scope": "machine", "user_id": uid})
                g = await c.post(f"{url}/memory/search", headers=headers, json={
                    "namespace": ns, "query": "client doctor roundtrip",
                    "scope": "machine", "user_id": uid, "limit": 1})
                await c.post(f"{url}/memory/forget", headers=headers, json={
                    "namespace": ns, "key": _PROBE_KEY, "scope": "machine", "user_id": uid})
                hits = len(g.json().get("results", [])) if g.status_code == 200 else 0
                if s.status_code == 200 and g.status_code == 200 and hits >= 1:
                    results.append(_line(PASS, "Memory roundtrip: store → search → forget OK."))
                else:
                    results.append(_line(FAIL,
                        f"Memory roundtrip incomplete (set={s.status_code} "
                        f"search={g.status_code} hits={hits}).",
                        "Check the token's write permission on the namespace."))
            except Exception as e:  # noqa: BLE001
                results.append(_line(FAIL, f"Memory roundtrip errored ({type(e).__name__})."))

    print("engram client doctor — can this box operate memory?\n")
    icon = {PASS: "✓", WARN: "⚠", FAIL: "✗"}
    fails = warns = 0
    for level, msg, fix in results:
        print(f"  [{icon[level]} {level}] {msg}")
        if fix:
            print(f"          fix: {fix}")
        fails += level == FAIL
        warns += level == WARN
    print()
    if fails:
        print(f"Result: {fails} FAIL — this box cannot fully operate memory yet.")
        return 1
    print(f"Result: OK{f' with {warns} warning(s)' if warns else ''} — memory operation works.")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
