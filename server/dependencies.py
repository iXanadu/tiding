"""FastAPI dependency helpers for principal-based access control."""

import logging

from fastapi import HTTPException, Request

from server.config import settings

logger = logging.getLogger(__name__)


def get_current_principal(request: Request) -> dict | None:
    """Extract principal from request.state (set by middleware). Returns None if anonymous."""
    return getattr(request.state, "principal", None)


def require_principal(request: Request) -> dict:
    """Require an authenticated principal. Raises 401 if anonymous."""
    principal = get_current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return principal


def require_admin(request: Request) -> dict:
    """Require an admin principal. Raises 401/403 as appropriate."""
    principal = require_principal(request)
    if not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return principal


def admin_or_open(request: Request) -> dict | None:
    """Require admin when require_auth=true, pass through otherwise.

    Used for admin endpoints: same gating as existing ENGRAM_API_TOKEN pattern,
    but upgraded to principal-aware when enforcement is on.
    """
    if settings.require_auth:
        return require_admin(request)
    return get_current_principal(request)


def check_namespace_access(
    principal: dict | None,
    namespace: str,
    mode: str,
) -> None:
    """Check if principal has access to namespace. No-op when principal is None.

    Args:
        principal: The authenticated principal dict, or None (anonymous).
        namespace: The namespace being accessed.
        mode: "read" or "write".

    Raises:
        HTTPException(403) if the principal lacks access.
    """
    if principal is None:
        return
    if not settings.require_auth:
        return
    if principal.get("is_admin"):
        return

    if mode == "read":
        allowed = principal.get("read_namespaces", [])
    elif mode == "write":
        allowed = principal.get("write_namespaces", [])
    else:
        raise ValueError(f"Invalid mode: {mode}")

    if "*" in allowed or namespace in allowed:
        return

    logger.warning(
        "PERMISSION DENIED: principal=%s mode=%s namespace=%s",
        principal.get("name"),
        mode,
        namespace,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Principal '{principal.get('name')}' lacks {mode} access to namespace '{namespace}'.",
    )
