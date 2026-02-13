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
    - Exempt paths: /health, /dashboard*
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from server.config import settings

logger = logging.getLogger(__name__)


class PrincipalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always allow exempt paths without auth
        path = request.url.path
        if path == "/health" or path.startswith("/dashboard"):
            request.state.principal = None
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
            request.state.principal = None
            return await call_next(request)

        # Try principal lookup first
        from server.services.principal_service import get_principal_by_token
        principal = await get_principal_by_token(token)
        if principal:
            request.state.principal = principal
            return await call_next(request)

        # Fall back to legacy ENGRAM_API_TOKEN comparison
        if settings.api_token and token == settings.api_token:
            request.state.principal = None
            return await call_next(request)

        return self._reject(request, "Invalid API token.")

    def _reject(self, request: Request, detail: str) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        logger.warning(
            "AUTH FAILED: %s from %s on %s %s",
            detail,
            client,
            request.method,
            request.url.path,
        )
        return JSONResponse(status_code=401, content={"detail": detail})
