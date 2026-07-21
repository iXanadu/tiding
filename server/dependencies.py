"""FastAPI dependency helpers for principal-based access control."""

import logging

from fastapi import HTTPException, Request

from server.config import canonical_namespace, settings

logger = logging.getLogger(__name__)


async def resolve_read_namespaces(principal: dict | None) -> list[str]:
    """Expand a principal's read_namespaces to a concrete list.

    Used when a search request omits namespace/namespaces — the server
    falls back to "everything the caller can read." Raises 401 if there
    is no principal (we can't resolve permissions without identity).
    Raises 403 if the principal has no read access anywhere.
    """
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="Search without explicit namespace requires an authenticated principal.",
        )
    read_perms = principal.get("read_namespaces") or []
    if "*" in read_perms or principal.get("is_admin"):
        from server.services.admin_service import list_namespaces
        all_ns = await list_namespaces()
        return all_ns
    if not read_perms:
        raise HTTPException(
            status_code=403,
            detail=f"Principal '{principal.get('name')}' has no read namespace permissions.",
        )
    # NS-1: expand grants in canonical space (dedup preserves order)
    return list(dict.fromkeys(canonical_namespace(ns) for ns in read_perms))


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


def _loopback_bind() -> bool:
    """True when the server is bound to a loopback-only address."""
    return settings.host in ("127.0.0.1", "::1", "localhost")


def admin_or_open(request: Request) -> dict | None:
    """Gate for /admin/* and principal CRUD.

    - require_auth=true → admin principal required (as always).
    - require_auth=false + loopback bind → open (operator's own box; the
      documented local posture).
    - require_auth=false + NON-loopback bind → the admin surface must not be
      anonymous: accept an admin principal or the legacy ENGRAM_API_TOKEN
      (its holder is the operator). Anonymous → 401; non-admin principal → 403.
      Closes the anonymous-admin hole from the 2026-07-21 audit.
    """
    if settings.require_auth:
        return require_admin(request)

    principal = get_current_principal(request)
    if _loopback_bind():
        return principal

    auth_source = getattr(request.state, "auth_source", "anonymous")
    if auth_source == "legacy":
        return principal
    if principal is not None:
        if principal.get("is_admin"):
            return principal
        raise HTTPException(status_code=403, detail="Admin access required.")
    raise HTTPException(
        status_code=401,
        detail=(
            "Admin endpoints require a token on a non-loopback bind. "
            "Use an admin principal token or ENGRAM_API_TOKEN."
        ),
    )


def check_namespaces_access(
    principal: dict | None,
    namespaces: list[str],
    mode: str,
) -> None:
    """Check access for multiple namespaces. Raises 403 on first failure."""
    for ns in namespaces:
        check_namespace_access(principal, ns, mode)


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

    # NS-1: compare in canonical namespace space so a principal whose grants
    # still name a legacy alias keeps working through a rename.
    namespace = canonical_namespace(namespace)
    allowed_canon = {canonical_namespace(a) for a in allowed}
    if "*" in allowed or namespace in allowed_canon:
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
