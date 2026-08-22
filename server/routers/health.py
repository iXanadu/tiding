import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from server.db import get_pool
from server.embeddings import check_health as check_embeddings

router = APIRouter(tags=["health"])

SERVER_VERSION = "0.2.0"


def _server_sha() -> str:
    """The git hash of the tree THIS server process imported, computed once
    at process start. That is exactly right for the question "is my fix
    live?": the server must restart to run new code, and a restart recomputes
    this. (VER-1, 2026-08-22: until now /health served no version at all, and
    the bridge's memory_status printed its OWN import-time hash labelled
    "Server version" — two readers on two days concluded a deploy had not
    landed when it had.)"""
    try:
        root = Path(__file__).resolve().parents[2]
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return "unknown"


SERVER_SHA = _server_sha()
SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.get("/health")
async def health():
    checks = {"postgres": False, "embeddings": False}

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = True
    except Exception:
        pass

    checks["embeddings"] = await check_embeddings()

    ok = all(checks.values())
    return {
        "status": "ok" if ok else "degraded",
        "checks": checks,
        # VER-1: the running server's own identity, additive (WIRE-1).
        "version": SERVER_VERSION,
        "sha": SERVER_SHA,
        "started_at": SERVER_STARTED_AT,
    }
