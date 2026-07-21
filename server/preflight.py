"""engram preflight — deployment self-check ("doctor").

    python -m server.preflight

Read-only. Validates the things that silently break a deployment: an insecure
or self-refusing bind, a Host allowlist that won't cover how clients actually
reach this box (the fleet-outage class), a missing database or embedding model,
and world-readable secrets. Exits non-zero if any check FAILs, so install/start
can gate on it.

Each check yields (level, message, fix): PASS / WARN / FAIL.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
from pathlib import Path

from server.config import settings

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def _host_allowed(host: str, patterns: list[str]) -> bool:
    """Mirror Starlette TrustedHostMiddleware matching (exact + *.suffix + *)."""
    host = host.split(":")[0].lower()
    for p in patterns:
        p = p.strip().lower()
        if p == "*" or host == p:
            return True
        if p.startswith("*.") and (host == p[2:] or host.endswith(p[1:])):
            return True
    return False


def _local_reachable_names() -> list[str]:
    """Best-effort: the names/IPs a client on the network could use for this box."""
    names: set[str] = set()
    try:
        hn = socket.gethostname()
        names.add(hn)
        names.add(hn.split(".")[0])
        if not hn.endswith(".local"):
            names.add(hn.split(".")[0] + ".local")
        names.add(socket.getfqdn())
    except OSError:
        pass
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, fam):
                ip = info[4][0]
                if ip and not ip.startswith(("127.", "::ffff:", "fe80:")) and ip != "::1":
                    names.add(ip)
        except OSError:
            pass
    # dedupe case-insensitively, drop empties
    seen, out = set(), []
    for n in sorted(names):
        if n and n.lower() not in seen:
            seen.add(n.lower()); out.append(n)
    return out


# --- checks -----------------------------------------------------------------

def check_bind_security():
    host = settings.host
    loopback = host in _LOOPBACK and host != "0.0.0.0"
    if loopback:
        return (PASS, f"Bind: loopback ({host}) — not network-reachable.", "")
    # non-loopback (0.0.0.0 or a specific external IP)
    if settings.require_auth or settings.api_token or settings.allow_insecure_bind:
        how = ("auth required" if settings.require_auth
               else "legacy api_token" if settings.api_token
               else "ALLOW_INSECURE_BIND opt-out")
        return (PASS, f"Bind: network ({host}) with {how}.", "")
    return (FAIL,
            f"Bind: ENGRAM_HOST={host} is network-reachable with NO auth — the "
            f"server will REFUSE TO START.",
            "Set ENGRAM_REQUIRE_AUTH=true (recommended), or "
            "ENGRAM_ALLOW_INSECURE_BIND=true only on a trusted private net.")


def check_trusted_hosts():
    """The fleet-outage check: bound to a network but the Host allowlist won't
    cover how clients actually reach this box → they get HTTP 400."""
    host = settings.host
    if host in _LOOPBACK and host != "0.0.0.0":
        return (PASS, "Host allowlist: loopback bind, default allowlist is fine.", "")
    patterns = settings.trusted_hosts_list()
    uncovered = [n for n in _local_reachable_names() if not _host_allowed(n, patterns)]
    if not uncovered:
        return (PASS, "Host allowlist: this box's names/IPs are covered.", "")
    return (WARN,
            f"Host allowlist: bound to {host}, but clients reaching this box as "
            f"{', '.join(uncovered)} will get HTTP 400 (rebinding guard).",
            f"Add them: ENGRAM_TRUSTED_HOSTS={','.join(patterns + uncovered)} "
            f"(include every hostname/IP clients use, e.g. a Tailscale name).")


async def check_database():
    try:
        import asyncpg
    except ImportError:
        return (FAIL, "Database: asyncpg not installed.", "pip install -e .")
    try:
        conn = await asyncpg.connect(settings.dsn, timeout=5)
    except Exception as e:
        return (FAIL, f"Database: cannot connect ({type(e).__name__}).",
                f"Check ENGRAM_DB_* and that PostgreSQL is running. "
                f"scripts/bootstrap-db.sh sets it up. ({e})")
    try:
        for ext in ("vector", "pg_trgm"):
            ok = await conn.fetchval(
                "SELECT 1 FROM pg_available_extensions WHERE name=$1", ext)
            if not ok:
                return (FAIL, f"Database: extension '{ext}' not available.",
                        "Install pgvector (image pgvector/pgvector, or the "
                        "pgvector package); pg_trgm ships with contrib.")
        return (PASS, "Database: reachable, pgvector + pg_trgm available.", "")
    finally:
        await conn.close()


def check_embedding_model():
    model = settings.embed_model
    hf = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    slug = "models--" + model.replace("/", "--")
    if (Path(hf) / "hub" / slug).exists():
        return (PASS, f"Embedding model cached ({model}).", "")
    return (WARN,
            f"Embedding model not cached ({model}). First start downloads "
            f"~270MB; on an OFFLINE box the service crash-loops.",
            "Warm it online once: python -c \"from sentence_transformers import "
            "SentenceTransformer as S; S('%s', trust_remote_code=True)\"" % model)


def check_secret_perms():
    findings = []
    def _bad(p: Path) -> bool:
        try:
            return bool(p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
        except OSError:
            return False
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists() and _bad(env):
        findings.append(str(env))
    idir = Path(os.path.expanduser("~/.config/engram/identities"))
    if idir.is_dir():
        for f in idir.iterdir():
            if f.is_file() and _bad(f):
                findings.append(str(f))
    keys = Path(os.path.expanduser("~/.config/engram.keys"))
    if keys.exists() and _bad(keys):
        findings.append(str(keys))
    if findings:
        return (WARN, f"Secret files group/world-readable: {', '.join(findings)}.",
                "chmod 600 " + " ".join(findings))
    return (PASS, "Secret file permissions: owner-only (or none present).", "")


def check_auth_posture():
    if settings.require_auth:
        return (PASS, "Auth: require_auth=true (principal tokens enforced).", "")
    return (WARN,
            "Auth: require_auth=false — anonymous access, and admin/principal "
            "endpoints are open. Fine ONLY on a trusted loopback box.",
            "Set ENGRAM_REQUIRE_AUTH=true before binding to any network.")


async def run() -> int:
    results = [
        check_bind_security(),
        check_trusted_hosts(),
        await check_database(),
        check_embedding_model(),
        check_secret_perms(),
        check_auth_posture(),
    ]
    print("engram preflight — deployment self-check\n")
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
        print(f"Result: {fails} FAIL, {warns} WARN — fix the FAILs before starting.")
        return 1
    if warns:
        print(f"Result: OK with {warns} warning(s) — review before exposing to a network.")
        return 0
    print("Result: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
