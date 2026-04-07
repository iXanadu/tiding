import logging

from fastapi import APIRouter, HTTPException, Request

from server.dependencies import check_namespace_access, check_namespaces_access, get_current_principal
from server.models import (
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryGetRequest,
    MemoryGetResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySetRequest,
    MemorySetResponse,
)
from server.services.memory_service import (
    memory_forget,
    memory_get,
    memory_search,
    memory_set,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


@router.post("/set", response_model=MemorySetResponse)
async def set_memory(req: MemorySetRequest, request: Request):
    logger.debug(f"SET ns={req.namespace} key={req.key} user_id={req.user_id} scope={req.scope}")
    principal = get_current_principal(request)
    check_namespace_access(principal, req.namespace, "write")
    metadata = {}
    if principal:
        metadata["principal"] = principal["name"]
    machine = request.headers.get("x-engram-machine")
    if machine:
        metadata["machine"] = machine
    try:
        key = await memory_set(
            namespace=req.namespace,
            key=req.key,
            value=req.value,
            scope=req.scope,
            user_id=req.user_id,
            tags=req.tags,
            tags_search=req.tags_search,
            expiration_days=req.expiration_days,
            metadata=metadata or None,
        )
        return MemorySetResponse(status="ok", key=key)
    except Exception as e:
        logger.exception("memory_set failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/get", response_model=MemoryGetResponse)
async def get_memory(req: MemoryGetRequest, request: Request):
    logger.debug(f"GET ns={req.namespace} key={req.key} scope={req.scope} user_id={req.user_id}")
    check_namespace_access(get_current_principal(request), req.namespace, "read")
    try:
        item = await memory_get(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
        )
        if item:
            return MemoryGetResponse(status="ok", memory=item)
        return MemoryGetResponse(status="not_found")
    except Exception as e:
        logger.exception("memory_get failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(req: MemorySearchRequest, request: Request):
    ns_list = req.resolved_namespaces()
    logger.debug(f"SEARCH ns={ns_list} query={req.query!r} user_id={req.user_id} scope={req.scope}")
    check_namespaces_access(get_current_principal(request), ns_list, "read")
    try:
        results = await memory_search(
            namespaces=ns_list,
            query=req.query,
            scope=req.scope,
            user_id=req.user_id,
            limit=req.limit,
        )
        return MemorySearchResponse(status="ok", results=results)
    except Exception as e:
        logger.exception("memory_search failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/forget", response_model=MemoryForgetResponse)
async def forget_memory(req: MemoryForgetRequest, request: Request):
    logger.debug(f"FORGET ns={req.namespace} key={req.key} scope={req.scope} user_id={req.user_id}")
    check_namespace_access(get_current_principal(request), req.namespace, "write")
    try:
        deleted = await memory_forget(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
        )
        status = "ok" if deleted else "not_found"
        return MemoryForgetResponse(status=status, key=req.key)
    except Exception as e:
        logger.exception("memory_forget failed")
        raise HTTPException(status_code=500, detail=str(e))
