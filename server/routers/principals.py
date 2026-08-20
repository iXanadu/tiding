"""Principal CRUD endpoints. Gated by admin_or_open (same pattern as /admin/*)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from server.dependencies import admin_or_open
from server.models import (
    AliasCreate,
    AliasResponse,
    PrincipalCreate,
    PrincipalCreateResponse,
    PrincipalListResponse,
    PrincipalResponse,
    PrincipalUpdate,
    TokenResponse,
)
from server.services import principal_service as ps
from server.services.audit_service import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/principals", tags=["principals"])


@router.post("", response_model=PrincipalCreateResponse)
async def create_principal(
    req: PrincipalCreate,
    _caller=Depends(admin_or_open),
):
    logger.info("CREATE principal name=%s type=%s", req.name, req.type)
    try:
        principal, raw_token = await ps.create_principal(
            name=req.name,
            type=req.type,
            is_admin=req.is_admin,
            password=req.password,
            token=req.token,
            read_namespaces=req.read_namespaces,
            write_namespaces=req.write_namespaces,
        )
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Principal '{req.name}' already exists.")
        logger.exception("create_principal failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")
    return PrincipalCreateResponse(
        status="ok",
        principal=PrincipalResponse(**principal),
        raw_token=raw_token,
    )


@router.get("", response_model=PrincipalListResponse)
async def list_principals(
    type: str | None = Query(None),
    active_only: bool = Query(True),
    _caller=Depends(admin_or_open),
):
    principals = await ps.list_principals(type=type, active_only=active_only)
    return PrincipalListResponse(
        status="ok",
        principals=[PrincipalResponse(**p) for p in principals],
    )


@router.get("/{name}", response_model=PrincipalResponse)
async def get_principal(
    name: str,
    _caller=Depends(admin_or_open),
):
    principal = await ps.get_principal(name)
    if not principal:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found.")
    return PrincipalResponse(**principal)


@router.patch("/{name}", response_model=PrincipalResponse)
async def update_principal(
    name: str,
    req: PrincipalUpdate,
    _caller=Depends(admin_or_open),
):
    updated, _ = await ps.update_principal(
        name=name,
        is_admin=req.is_admin,
        password=req.password,
        token=req.token,
        read_namespaces=req.read_namespaces,
        write_namespaces=req.write_namespaces,
        active=req.active,
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found.")
    # AUDIT-2. Record WHICH fields moved, never their values — an audit row
    # that leaks a token is worse than no audit row.
    await audit(
        action="principal.update",
        principal=_caller,
        detail={
            "target": name,
            "fields": sorted(
                f for f, v in (
                    ("is_admin", req.is_admin), ("password", req.password),
                    ("token", req.token), ("read_namespaces", req.read_namespaces),
                    ("write_namespaces", req.write_namespaces), ("active", req.active),
                ) if v is not None
            ),
        },
        target_principal_id=updated.get("id"),
    )
    return PrincipalResponse(**updated)


@router.delete("/{name}")
async def deactivate_principal(
    name: str,
    _caller=Depends(admin_or_open),
):
    result = await ps.deactivate_principal(name)
    if not result:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found or already inactive.")
    await audit(
        action="principal.deactivate",
        principal=_caller,
        detail={"target": name},
    )
    return {"status": "ok", "name": name, "active": False}


@router.post("/{name}/token", response_model=TokenResponse)
async def regenerate_token(
    name: str,
    _caller=Depends(admin_or_open),
):
    raw, _ = await ps.generate_token()
    updated, _ = await ps.update_principal(name=name, token=raw)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found.")
    # THE row that was missing. A rotation is the event whose date nobody
    # could answer during the 2026-08-16 incident. The raw token is NEVER
    # recorded — only that a rotation happened, by whom, and when.
    await audit(
        action="principal.token_regenerate",
        principal=_caller,
        detail={"target": name},
        target_principal_id=updated.get("id"),
    )
    return TokenResponse(status="ok", principal_name=name, raw_token=raw)


@router.post("/{name}/aliases", response_model=AliasResponse)
async def add_alias(
    name: str,
    req: AliasCreate,
    _caller=Depends(admin_or_open),
):
    alias = await ps.add_alias(name, req.alias, req.source)
    if not alias:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found.")
    return AliasResponse(**alias)


@router.get("/{name}/aliases")
async def list_aliases(
    name: str,
    _caller=Depends(admin_or_open),
):
    principal = await ps.get_principal(name)
    if not principal:
        raise HTTPException(status_code=404, detail=f"Principal '{name}' not found.")
    aliases = await ps.list_aliases(name)
    return {"status": "ok", "aliases": [AliasResponse(**a) for a in aliases]}


@router.delete("/{name}/aliases")
async def remove_alias(
    name: str,
    req: AliasCreate,
    _caller=Depends(admin_or_open),
):
    # Scope to the principal named in the path — without it, this endpoint
    # could delete an alias belonging to a DIFFERENT principal (alias hijack).
    removed = await ps.remove_alias(req.alias, req.source, principal_name=name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Alias not found.")
    return {"status": "ok", "alias": req.alias, "source": req.source}
