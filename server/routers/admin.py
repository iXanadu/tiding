import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from server.config import settings
from server.dependencies import admin_or_open
from server.models import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    CleanupResponse,
    MemoryListResponse,
    MemoryStatsResponse,
    MemoryUpdateRequest,
    MemoryUpdateResponse,
)
from server.services.admin_service import (
    bulk_delete,
    cleanup_expired,
    get_stats,
    list_memories,
    update_memory,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories_endpoint(
    namespace: str | None = Query(None),
    scope: str | None = Query(None),
    user_id: str | None = Query(None),
    key_prefix: str | None = Query(None),
    search: str | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    sort_by: str = Query("key"),
    sort_order: str = Query("asc"),
    include_value: bool = Query(False),
    value_max_length: int = Query(200, ge=1, le=10000),
    _caller=Depends(admin_or_open),
):
    logger.debug(f"LIST ns={namespace} scope={scope} prefix={key_prefix} search={search}")
    try:
        total, items = await list_memories(
            namespace=namespace,
            scope=scope,
            user_id=user_id,
            key_prefix=key_prefix,
            search=search,
            created_after=created_after,
            created_before=created_before,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            sort_order=sort_order,
            include_value=include_value,
            value_max_length=value_max_length,
        )
        return MemoryListResponse(
            status="ok", total=total, offset=offset, limit=limit, items=items
        )
    except Exception as e:
        logger.exception("list_memories failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/memories", response_model=MemoryUpdateResponse)
async def update_memory_endpoint(
    req: MemoryUpdateRequest,
    _caller=Depends(admin_or_open),
):
    logger.debug(f"UPDATE ns={req.namespace} key={req.key} scope={req.scope}")
    try:
        updated = await update_memory(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
            new_namespace=req.new_namespace,
            new_scope=req.new_scope,
            new_user_id=req.new_user_id,
            new_key=req.new_key,
            new_tags=req.new_tags,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Memory not found or no changes")
        return MemoryUpdateResponse(status="ok")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("update_memory failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats", response_model=MemoryStatsResponse)
async def stats_endpoint(
    namespace: str | None = Query(None),
    by_scope: bool = Query(False),
    _caller=Depends(admin_or_open),
):
    logger.debug(f"STATS ns={namespace} by_scope={by_scope}")
    try:
        stats = await get_stats(namespace=namespace, by_scope=by_scope)
        return MemoryStatsResponse(status="ok", stats=stats)
    except Exception as e:
        logger.exception("get_stats failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_endpoint(
    req: BulkDeleteRequest,
    _caller=Depends(admin_or_open),
):
    logger.debug(f"BULK DELETE ns={req.namespace} prefix={req.key_prefix}")
    try:
        deleted = await bulk_delete(
            namespace=req.namespace,
            key_prefix=req.key_prefix,
            scope=req.scope,
            user_id=req.user_id,
            older_than_days=req.older_than_days,
        )
        return BulkDeleteResponse(status="ok", deleted_count=deleted)
    except Exception as e:
        logger.exception("bulk_delete failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup", response_model=CleanupResponse)
async def cleanup_endpoint(
    batch_size: int = Query(None, ge=1, le=10000),
    _caller=Depends(admin_or_open),
):
    logger.debug("CLEANUP triggered manually")
    try:
        size = batch_size or settings.cleanup_batch_size
        deleted = await cleanup_expired(batch_size=size)
        return CleanupResponse(status="ok", deleted_count=deleted)
    except Exception as e:
        logger.exception("cleanup_expired failed")
        raise HTTPException(status_code=500, detail=str(e))
