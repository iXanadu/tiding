import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.auth import PrincipalAuthMiddleware
from server.config import settings
from server.db import close_pool, init_pool
from server.embeddings import close_client, init_client
from server.routers import (
    admin,
    dashboard,
    health,
    identity,
    memory,
    principals,
    session,
)
from server.services.cleanup_task import (
    expiration_cleanup_loop,
    inbox_autoresolve_loop,
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _bootstrap_admin():
    """Auto-create _bootstrap admin principal when require_auth=true and no admins exist.

    Uses ENGRAM_API_TOKEN as the raw token. Idempotent — catches UniqueViolation
    if _bootstrap already exists.
    """
    if not settings.require_auth or not settings.api_token:
        return

    from server.services.principal_service import create_principal, list_principals

    admins = await list_principals(type=None, active_only=True)
    if any(p["is_admin"] for p in admins):
        return

    logger.warning(
        "require_auth=true but no admin principals exist. "
        "Auto-creating '_bootstrap' admin from ENGRAM_API_TOKEN."
    )
    try:
        import asyncpg
        await create_principal(
            name="_bootstrap",
            type="agent",
            is_admin=True,
            token=settings.api_token,
            read_namespaces=["*"],
            write_namespaces=["*"],
        )
        logger.warning(
            "_bootstrap admin created. Use its token (ENGRAM_API_TOKEN) to create "
            "real principals, then deactivate _bootstrap."
        )
    except asyncpg.UniqueViolationError:
        logger.info("_bootstrap admin already exists (idempotent).")


def check_bind_security(host: str, require_auth: bool, api_token: str,
                        allow_insecure_bind: bool) -> None:
    """SEC-1 secure-by-default gate. Refuse to serve a network-reachable,
    unauthenticated memory store unless the operator explicitly opts out.

    Loopback binds are always fine (tokenless local use is the personal
    default). A non-loopback bind requires EITHER auth configured
    (require_auth or an api_token) OR the explicit trusted-network opt-out
    ENGRAM_ALLOW_INSECURE_BIND=true (e.g. a Tailscale-only LAN).
    """
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if loopback or require_auth or api_token or allow_insecure_bind:
        return
    raise RuntimeError(
        f"REFUSING TO START: ENGRAM_HOST={host!r} exposes an UNAUTHENTICATED "
        "memory store beyond this machine. An open engram on a reachable "
        "network lets anyone read and write your agents' memory.\n"
        "Pick ONE:\n"
        "  1. Bind loopback (default):        ENGRAM_HOST=127.0.0.1\n"
        "  2. Turn on auth:                   ENGRAM_REQUIRE_AUTH=true (+ principal tokens)\n"
        "     or set a shared token:          ENGRAM_API_TOKEN=<token>\n"
        "  3. Trusted private network ONLY:   ENGRAM_ALLOW_INSECURE_BIND=true\n"
        "     (e.g. Tailscale/WireGuard — never on a public interface)"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting engram service")
    check_bind_security(settings.host, settings.require_auth,
                        settings.api_token, settings.allow_insecure_bind)
    if settings.require_auth:
        logger.info("Principal-based authentication ENFORCED (require_auth=true)")
    elif settings.api_token:
        logger.info("API token authentication ENABLED")
    else:
        logger.info("API token authentication DISABLED (set ENGRAM_API_TOKEN to enable)")
    if settings.warn_unauthed:
        logger.info("WARN MODE: unauthenticated requests will be logged")
    await init_pool()
    await init_client()
    logger.info("Database pool and embedding client ready")

    await _bootstrap_admin()

    background_tasks = []
    if settings.cleanup_enabled:
        background_tasks.append(asyncio.create_task(expiration_cleanup_loop()))
    if settings.inbox_autoresolve_enabled:
        background_tasks.append(asyncio.create_task(inbox_autoresolve_loop()))
    yield
    logger.info("Shutting down")
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_client()
    await close_pool()


app = FastAPI(
    title="engram",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(PrincipalAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://claude.ai"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Anti-DNS-rebinding: reject forged Host headers before routing. CORS does not
# stop rebinding (the malicious page becomes same-origin); a Host allowlist
# does. Starlette's TrustedHostMiddleware runs outermost (added last).
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts_list()
)

app.include_router(memory.router)
app.include_router(session.router)
app.include_router(admin.router)
app.include_router(principals.router)
app.include_router(identity.router)
app.include_router(health.router)
app.include_router(dashboard.router)

# Pinned, locally-built dashboard assets (Alpine + compiled Tailwind) —
# no CDN at runtime (2026-07-21 audit). Built by scripts/build-dashboard-assets.sh.
from pathlib import Path  # noqa: E402

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)


if __name__ == "__main__":
    # Prefer `python -m server` (see server/__main__.py) so guard-input equals
    # the real bind. This fallback keeps `python server/main.py` working.
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
