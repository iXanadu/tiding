"""End-to-end check of the front door against `wrangler dev`.

Plays a hosted MCP client: dynamic registration, authorize with PKCE, the
owner's consent form, code exchange, MCP calls, refresh, revocation. Stdlib
only. Usage: python3 test/roundtrip.py [base_url]   (reads .dev.vars, or $FRONTDOOR_VARS)
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787"
REDIRECT = "http://localhost:9/callback"
DEV = dict(l.split("=", 1) for l in open(os.environ.get("FRONTDOOR_VARS", ".dev.vars")).read().split() if "=" in l)
PASS = DEV["OWNER_PASSPHRASE"]

jar = http.cookiejar.CookieJar()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
failures = []


def req(method, path, data=None, headers=None, form=False):
    h = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path if path.startswith("/") else path, body, h, method=method)
    try:
        with opener.open(r) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def authorize(passphrase):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": "mcp",
        "state": "st8", "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": BASE + "/mcp",
    })
    s, _, page = req("GET", "/authorize?" + q)
    m = re.search(r'name="handle" value="([^"]+)"', page)
    if s != 200 or not m:
        return s, None, verifier, page
    if BASE.startswith("http://"):  # __Host- cookies are Secure; local dev is plain http
        for c in jar:
            c.secure = False
    s, h, body = req("POST", "/authorize?" + q, {
        "handle": m.group(1), "decision": "approve", "principal": "frontdoor-probe", "passphrase": passphrase,
    }, form=True)
    return s, h.get("Location"), verifier, body


# 1. dynamic client registration
s, _, body = req("POST", "/oauth/register", {
    "client_name": "roundtrip-test", "redirect_uris": [REDIRECT],
    "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
})
check("DCR registers a public client", s == 201, f"{s} {body[:200]}")
client_id = json.loads(body)["client_id"]

# 2. wrong passphrase mints nothing
s, loc, _, body = authorize("wrong-" + PASS)
check("wrong passphrase is refused (403, no redirect)", s == 403 and not loc, f"{s} {loc}")

# 3. right passphrase -> code
s, loc, verifier, body = authorize(PASS)
check("consent approve redirects with a code", s == 302 and loc and "code=" in loc, f"{s} {body[:200]}")
qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
check("redirect carries state and iss", qs.get("state") == ["st8"] and "iss" in qs, str(qs))
code = qs["code"][0]

# 4. code exchange (PKCE)
tok_form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier, "resource": BASE + "/mcp"}
s, _, body = req("POST", "/oauth/token", tok_form, form=True)
check("code exchange issues tokens", s == 200 and "access_token" in body, f"{s} {body[:200]}")
tokens = json.loads(body)


# 5. MCP calls
def mcp(method, params=None, token=None, rid=1):
    s, h, body = req("POST", "/mcp", {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                     {"Authorization": f"Bearer {token or tokens['access_token']}",
                      "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
    return s, (json.loads(body) if body.startswith("{") else body)


def tool(name, args=None):
    s, r = mcp("tools/call", {"name": name, "arguments": args or {}})
    res = r.get("result", {}) if isinstance(r, dict) else {}
    return res.get("isError", True), (res.get("content") or [{}])[0].get("text", str(r))


s, r = mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "roundtrip", "version": "0"}})
check("MCP initialize", s == 200 and "result" in r, f"{s} {str(r)[:200]}")
s, r = mcp("tools/list")
names = sorted(t["name"] for t in r["result"]["tools"])
check("ten tools listed", len(names) == 10, str(names))

err, text = tool("whoami")
check("whoami maps to the approved principal", not err and '"frontdoor-probe"' in text, text[:200])
err, text = tool("set", {"namespace": "frontdoor-probe", "key": "probe/roundtrip", "value": "front door works",
                         "scope": "shared"})
check("write to own namespace", not err, text[:200])
err, text = tool("get", {"namespace": "frontdoor-probe", "key": "probe/roundtrip", "scope": "shared"})
check("read it back", not err and "front door works" in text, text[:200])
err, text = tool("set", {"namespace": "ixanadu", "key": "probe/x", "value": "x", "scope": "shared"})
check("write outside grant is refused by the server", err and "403" in text, text[:200])
err, text = tool("inbox")
check("inbox lists", not err, text[:200])

s, r = mcp("tools/list", token="not-a-token")
check("bogus bearer is refused", s == 401, str(s))

# 6. replaying a used code fails AND revokes what it issued (OAuth 2.1 4.1.3)
s, _, _ = req("POST", "/oauth/token", tok_form, form=True)
check("a code works only once", s == 400, str(s))
s, r = mcp("tools/list")
check("code replay revoked the tokens it issued", s == 401, str(s))
s, loc, verifier, _ = authorize(PASS)
code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
s, _, body = req("POST", "/oauth/token", {**tok_form, "code": code, "code_verifier": verifier}, form=True)
tokens = json.loads(body)

# 7. refresh rotates; revoke kills the grant
s, _, body = req("POST", "/oauth/token", {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                          "client_id": client_id}, form=True)
check("refresh issues a new access token", s == 200 and "access_token" in body, f"{s} {body[:200]}")
fresh = json.loads(body)
s, _, _ = req("POST", "/oauth/token", {"token": fresh["access_token"], "client_id": client_id}, form=True)
check("revocation accepted", s == 200, str(s))
s, r = mcp("tools/list", token=fresh["access_token"])
check("revoked token is refused", s == 401, str(s))

# 8. kill switch: revoke-all refuses a wrong bearer, then revokes every grant
s, _, _ = req("POST", "/admin/revoke-all", {}, {"Authorization": "Bearer wrong"})
check("revoke-all refuses a wrong kill token", s == 403, str(s))
s, loc, verifier, _ = authorize(PASS)
code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
s, _, body = req("POST", "/oauth/token", {**tok_form, "code": code, "code_verifier": verifier}, form=True)
live = json.loads(body)
s, r = mcp("tools/list", token=live["access_token"])
check("a fresh grant works before the kill", s == 200, str(s))
s, _, body = req("POST", "/admin/revoke-all", {}, {"Authorization": f"Bearer {DEV['KILL_TOKEN']}"})
check("revoke-all revokes", s == 200 and json.loads(body).get("revoked", 0) >= 1, f"{s} {body}")
s, r = mcp("tools/list", token=live["access_token"])
check("killed grant's token is refused", s == 401, str(s))

print(f"\n{len(failures)} failure(s)" if failures else "\nALL PASS")
sys.exit(1 if failures else 0)
