"""OBS-REQLOG-1 middleware — records one row per request.

Registered OUTERMOST, deliberately. The interesting requests for this trail are
often the ones that never reach a route: a rejected credential (401/403 from
PrincipalAuthMiddleware) and a forged Host header (400 from TrustedHost) are
exactly the events an operator needs to see, and an inner middleware would be
short-circuited before it could record them.

Reading ``request.state`` AFTER ``call_next`` returns is what makes that work:
the inner auth middleware has populated it on the same request object by then,
so being outermost costs no principal attribution.
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware

from server.config import settings
from server.services.request_log import record_request, should_log
from server.services.mail_signal import redact_path

logger = logging.getLogger(__name__)

# Client-supplied, so bounded before it reaches the table — an unbounded
# header is a free write amplifier.
_MAX_PROVENANCE = 64


def _clip(value):
    """Trim a provenance header to something safe to store; None stays None."""
    if not value:
        return None
    return value.strip()[:_MAX_PROVENANCE] or None


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not settings.request_log_enabled or not should_log(request.url.path):
            return await call_next(request)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # In a finally so an unhandled exception downstream still leaves a
            # trace — a 500 that vanishes from the trail is the case you most
            # want recorded.
            duration_ms = int((time.perf_counter() - started) * 1000)
            principal = getattr(request.state, "principal", None)
            await record_request(
                principal=(principal or {}).get("name") if principal else None,
                auth_source=getattr(request.state, "auth_source", None),
                method=request.method,
                # PATH ONLY — url.path excludes the query string by
                # construction, so a value a client put in a URL cannot be
                # recorded here. Do not switch this to str(request.url).
                # WEBPUSH-1: a signal key lives in the path, so it is masked
                # before it can land in the table.
                path=redact_path(request.url.path),
                status=status,
                duration_ms=duration_ms,
                # Same header PUBLIC-SURFACE-2 keys on. Set by the edge only,
                # replaced (never merely appended) by proxy_set_header, so
                # its presence means "came in from the internet".
                via_public=bool(
                    settings.public_proxy_header
                    and request.headers.get(settings.public_proxy_header)
                ),
                # OBS-SESSION-1: provenance the bridge already sends on every
                # call. Recorded, never trusted — a client can set these, so
                # they answer "which box says it called" and are not evidence
                # against a caller that lies. That is enough for the question
                # they exist for: separating our own fleet's traffic by origin.
                machine=_clip(request.headers.get("x-engram-machine")),
                project=_clip(request.headers.get("x-engram-project")),
            )
