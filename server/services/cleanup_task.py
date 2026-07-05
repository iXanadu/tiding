import asyncio
import logging

from server.config import settings
from server.services.admin_service import cleanup_expired
from server.services.memory_service import inbox_autoresolve_stale

logger = logging.getLogger(__name__)


async def expiration_cleanup_loop() -> None:
    interval_seconds = settings.cleanup_interval_hours * 3600
    logger.info(
        "Expiration cleanup task started "
        f"(interval={settings.cleanup_interval_hours}h, batch={settings.cleanup_batch_size})"
    )
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                deleted = await cleanup_expired(batch_size=settings.cleanup_batch_size)
                if deleted:
                    logger.info(f"Scheduled cleanup removed {deleted} expired memories")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during scheduled expiration cleanup")
    except asyncio.CancelledError:
        logger.info("Expiration cleanup task cancelled")


async def inbox_autoresolve_loop() -> None:
    interval_seconds = settings.inbox_autoresolve_interval_hours * 3600
    logger.info(
        "Inbox stale auto-resolve task started "
        f"(interval={settings.inbox_autoresolve_interval_hours}h, "
        f"after={settings.inbox_autoresolve_after_hours}h)"
    )
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                resolved = await inbox_autoresolve_stale(
                    older_than_hours=settings.inbox_autoresolve_after_hours,
                )
                if resolved:
                    logger.info(
                        f"Inbox auto-resolve drained {resolved} read+stale messages"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error during inbox stale auto-resolve")
    except asyncio.CancelledError:
        logger.info("Inbox stale auto-resolve task cancelled")
