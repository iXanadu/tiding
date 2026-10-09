"""Principal-aware authentication middleware.

Two modes controlled by ENGRAM_REQUIRE_AUTH:

  false (default) — Enrichment mode:
    - If Bearer token matches a principal → request.state.principal = principal_dict
    - If Bearer token matches ENGRAM_API_TOKEN → request.state.principal = None (legacy compat)
    - If no token + no ENGRAM_API_TOKEN → anonymous, request.state.principal = None
    - If no token + ENGRAM_API_TOKEN set → 401

  true — Enforcement mode:
    - Bearer token MUST match a principal → request.state.principal = principal_dict
    - No token or unrecognized token → 401
    - Exempt paths: /health, /dashboard*, /bridge, /memory/signal/<key>
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from server.config import settings
from server.services.mail_signal import is_signal_path, redact_path

logger = logging.getLogger(__name__)


class PrincipalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always allow exempt paths without auth (/static: the dashboard's
        # own vendored JS/CSS — public by nature, no data behind it)
        path = request.url.path
        if (
            path == "/health"
            or path.startswith("/dashboard")
            or path == "/bridge"
            or path.startswith("/static/")
            # WEBPUSH-1: exactly /memory/signal/<one segment>, GET only. The
            # key in the path is the credential; anything else under
            # /memory/ — including /memory/signal itself — still needs a
            # bearer token.
            or (request.method == "GET" and is_signal_path(path))
        ):
            request.state.principal = None
            request.state.auth_source = "anonymous"
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else None

        if settings.require_auth:
            return await self._enforce(request, call_next, token)
        else:
            return await self._enrich(request, call_next, token)

    async def _enforce(self, request: Request, call_next, token: str | None):
        """Enforcement mode: require a valid principal token."""
        if not token:
            return self._reject(request, "Authentication required. Provide a Bearer token.")

        # Try principal lookup
        from server.services.principal_service import get_principal_by_token
        principal = await get_principal_by_token(token)
        if principal:
            request.state.principal = principal
            request.state.auth_source = "principal"
            refused = self._refuse_admin_on_public(request, principal)
            if refused is not None:
                return refused
            return await call_next(request)

        return self._reject(request, "Invalid or inactive token.")

    async def _enrich(self, request: Request, call_next, token: str | None):
        """Enrichment mode: identify principal if possible, fall back to legacy token check."""
        if not token:
            if settings.api_token:
                # Legacy behavior: api_token set but no Bearer header → 401
                return self._reject(
                    request,
                    "Authentication required. Set Authorization: Bearer <token> header.",
                )
            # No token configured, no token provided → anonymous
            if settings.warn_unauthed:
                self._warn_unauthed(request, "no token provided")
            request.state.principal = None
            request.state.auth_source = "anonymous"
            return await call_next(request)

        # Try principal lookup first
        from server.services.principal_service import get_principal_by_token
        principal = await get_principal_by_token(token)
        if principal:
            request.state.principal = principal
            request.state.auth_source = "principal"
            refused = self._refuse_admin_on_public(request, principal)
            if refused is not None:
                return refused
            return await call_next(request)

        # Fall back to legacy ENGRAM_API_TOKEN comparison
        if settings.api_token and token == settings.api_token:
            if settings.warn_unauthed:
                self._warn_unauthed(request, "legacy API token (no principal)")
            request.state.principal = None
            request.state.auth_source = "legacy"
            return await call_next(request)

        return self._reject(request, "Invalid API token.")

    def _refuse_admin_on_public(self, request: Request, principal: dict | None):
        """PUBLIC-SURFACE-2: an admin credential must not work from the open
        internet, even though the edge already hides /admin.

        check_namespace_access() short-circuits on is_admin by design, so an
        admin token pasted into any externally-hosted surface would carry
        unrestricted fleet-wide reach behind one bearer string. This is
        defence-in-depth at the layer where the operator cannot see what holds
        the token — not a response to a live incident.

        The marker is a header the public edge sets. SAFE AGAINST FORGERY BY
        CONSTRUCTION: presence of the header only ever REMOVES privilege, so a
        client that forges it merely denies itself. That is also why the app
        half can ship before the edge half — absent the header this is inert,
        which is exactly today's behaviour.
        """
        if not principal or not principal.get("is_admin"):
            return None
        header = settings.public_proxy_header
        if not header or not request.headers.get(header):
            return None
        client = request.client.host if request.client else "unknown"
        logger.warning(
            "ADMIN REFUSED ON PUBLIC SURFACE: principal=%s from %s on %s %s",
            principal.get("name"), client, request.method, redact_path(request.url.path),
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "Admin credentials are not accepted on the "
                               "public surface. Use a scoped principal."},
        )

    def _warn_unauthed(self, request: Request, reason: str) -> None:
        client = request.client.host if request.client else "unknown"
        logger.warning(
            "UNAUTHED REQUEST: %s %s from %s — %s",
            request.method,
            redact_path(request.url.path),
            client,
            reason,
        )

    def _reject(self, request: Request, detail: str) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        logger.warning(
            "AUTH FAILED: %s from %s on %s %s",
            detail,
            client,
            request.method,
            redact_path(request.url.path),
        )
        return JSONResponse(status_code=401, content={"detail": detail})
