import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.auth import PrincipalAuthMiddleware
from server.config import settings
from server.db import close_pool, init_pool
from server.embeddings import close_client, init_client
from server.routers import admin, dashboard, health, identity, memory, principals
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting engram service")
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

app.include_router(memory.router)
app.include_router(admin.router)
app.include_router(principals.router)
app.include_router(identity.router)
app.include_router(health.router)
app.include_router(dashboard.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
