"""Caller-scoped identity endpoints.

These let a client discover its own principal record and capabilities
without an admin token. The token in the Authorization header is the
only input — the server returns who that token represents and what
namespaces it can read/write.
"""

import logging

from fastapi import APIRouter, Depends

from server.dependencies import require_principal
from server.models import NamespacesResponse, PrincipalResponse
from server.services.admin_service import list_namespaces

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])


@router.get("/whoami", response_model=PrincipalResponse)
async def whoami(principal: dict = Depends(require_principal)):
    return PrincipalResponse(**principal)


@router.get("/namespaces", response_model=NamespacesResponse)
async def namespaces(principal: dict = Depends(require_principal)):
    read_perms = principal.get("read_namespaces") or []
    write_perms = principal.get("write_namespaces") or []

    if "*" in read_perms or "*" in write_perms:
        all_ns = await list_namespaces()
    else:
        all_ns = None

    read = all_ns if "*" in read_perms else list(read_perms)
    write = all_ns if "*" in write_perms else list(write_perms)
    return NamespacesResponse(status="ok", read=read, write=write)
