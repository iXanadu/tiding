import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from server.config import settings
from server.dependencies import admin_or_open
from server.services.audit_service import audit
from server.models import (
    BulkDeleteRequest,
    BulkDeleteResponse,
    CleanupResponse,
    DeletionQueueItem,
    DeletionQueueResponse,
    DeletionRejectRequest,
    DeletionRejectResponse,
    EstateTransferRequest,
    EstateTransferResponse,
    MemoryListResponse,
    MemoryStatsResponse,
    MemoryUpdateRequest,
    MemoryUpdateResponse,
)
from server.services.memory_service import (
    deletion_queue_list,
    deletion_queue_reject,
    estate_transfer,
)
from server.services.principal_service import get_principal
from server.services.admin_service import (
    bulk_delete,
    cleanup_expired,
    get_stats,
    list_machines,
    list_memories,
    update_memory,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories_endpoint(
    namespace: str | None = Query(None, description="Namespace(s) to list, comma-separated for multiple"),
    scope: str | None = Query(None),
    user_id: str | None = Query(None),
    key_prefix: str | None = Query(None),
    search: str | None = Query(None),
    machine: str | None = Query(None, description="Filter by metadata.machine"),
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
    ns_list = [ns.strip() for ns in namespace.split(",") if ns.strip()] if namespace else None
    logger.debug(f"LIST ns={ns_list} scope={scope} prefix={key_prefix} search={search}")
    try:
        total, items = await list_memories(
            namespaces=ns_list,
            scope=scope,
            user_id=user_id,
            key_prefix=key_prefix,
            search=search,
            machine=machine,
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
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.get("/machines")
async def list_machines_endpoint(_caller=Depends(admin_or_open)):
    machines = await list_machines()
    return {"machines": machines}


@router.patch("/memories", response_model=MemoryUpdateResponse)
async def update_memory_endpoint(
    req: MemoryUpdateRequest,
    _caller=Depends(admin_or_open),
):
    logger.debug(f"UPDATE ns={req.namespace} key={req.key} scope={req.scope}")
    # A non-admin principal that reaches this surface (enrichment mode) must
    # hold write access to BOTH the source namespace and any move target —
    # new_namespace previously escaped every write check (2026-07-21 audit).
    from server.dependencies import check_namespace_access
    check_namespace_access(_caller, req.namespace, "write", force=True)
    if req.new_namespace and req.new_namespace != req.namespace:
        check_namespace_access(_caller, req.new_namespace, "write", force=True)
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
        raise HTTPException(status_code=500, detail="internal error — see server logs")


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
        raise HTTPException(status_code=500, detail="internal error — see server logs")


# A prefix is "broad" when it does not identify a specific record — it names a
# CLASS. `inbox/` matches every message ever sent; `inbox/<uuid>` matches one.
# The 2026-07-23 incident was exactly this distinction going unnoticed, so the
# rule is deliberately crude and errs toward asking: a prefix that is empty,
# very short, or ends at a path separator gets the extra gate.
_BROAD_MIN_LEN = 12


def _is_broad(req) -> bool:
    p = (req.key_prefix or "").strip()
    if len(p) < _BROAD_MIN_LEN:
        return True
    if p.endswith(("/", ":", "-", "_")):
        return True
    return False


def _blast_label(req) -> str:
    """The exact string a caller must echo back to run a broad delete.

    Deliberately built from the request itself so it cannot be guessed from the
    docs alone — you have to have read the refusal for THIS call, which means
    you have seen the match count it carried.
    """
    return f"{req.namespace}:{req.key_prefix}"


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_endpoint(
    req: BulkDeleteRequest,
    _caller=Depends(admin_or_open),
):
    logger.debug(
        f"BULK DELETE ns={req.namespace} prefix={req.key_prefix} dry_run={req.dry_run}"
    )
    try:
        matched, sample = await bulk_delete(
            namespace=req.namespace,
            key_prefix=req.key_prefix,
            scope=req.scope,
            user_id=req.user_id,
            older_than_days=req.older_than_days,
            dry_run=True,
        )

        if req.dry_run:
            return BulkDeleteResponse(
                status="ok",
                deleted_count=0,
                matched_count=matched,
                dry_run=True,
                sample_keys=sample,
                guidance=(
                    f"DRY RUN — nothing was deleted. {matched} row(s) MATCH this "
                    f"predicate and WOULD be destroyed.\n"
                    f"Re-send with \"dry_run\": false to actually delete."
                    + (
                        f"\nThis prefix is broad: you must also send "
                        f'"i_understand_this_deletes": "{_blast_label(req)}".'
                        if _is_broad(req) else ""
                    )
                ),
            )

        # A broad predicate must be named, not stumbled into. Checked AFTER the
        # match count so the refusal can tell the caller what they nearly did.
        if _is_broad(req):
            expected = _blast_label(req)
            if (req.i_understand_this_deletes or "").strip() != expected:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"REFUSED: this predicate matches {matched} row(s) — a whole "
                        f"class of keys, not a specific one. To proceed, re-send with "
                        f'"i_understand_this_deletes": "{expected}". '
                        f"Prefer deleting exact keys one at a time."
                    ),
                )

        deleted, sample = await bulk_delete(
            namespace=req.namespace,
            key_prefix=req.key_prefix,
            scope=req.scope,
            user_id=req.user_id,
            older_than_days=req.older_than_days,
        )
        logger.warning(
            f"BULK DELETE EXECUTED ns={req.namespace} prefix={req.key_prefix} "
            f"deleted={deleted}"
        )
        # AUDIT-1: the largest-blast-radius write in the API. Executed
        # deletes only — dry runs destroy nothing and would bury the trail.
        await audit("admin.bulk_delete", _caller, {
            "namespace": req.namespace, "key_prefix": req.key_prefix,
            "scope": req.scope, "user_id": req.user_id,
            "older_than_days": req.older_than_days, "deleted": deleted,
        })
        return BulkDeleteResponse(
            status="ok",
            deleted_count=deleted,
            matched_count=deleted,
            dry_run=False,
            sample_keys=sample,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bulk_delete failed")
        raise HTTPException(status_code=500, detail="internal error — see server logs")


@router.post("/estate/transfer", response_model=EstateTransferResponse)
async def estate_transfer_endpoint(
    req: EstateTransferRequest,
    _caller=Depends(admin_or_open),
):
    """MEM-8: reassign a departed principal's destruction rights to an heir.

    Sets `custodian` on the rows the departed principal controls; `owner` is
    NEVER touched — attribution is immutable, a transferred row reads
    owner=<author>, custodian=<heir>. Correction needed no transfer
    (supersede is already open to successors); this moves only the right to
    destroy, deliberately, once, on the operator's word.
    """
    heir = await get_principal(req.to_principal.strip().lower())
    if heir is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"to_principal '{req.to_principal}' is not a known principal "
                f"— an estate cannot be transferred to a name that cannot "
                f"authenticate (typos here would strand destruction rights)."
            ),
        )
    try:
        rows = await estate_transfer(
            from_principal=req.from_principal,
            to_principal=req.to_principal,
            namespace=req.namespace,
            project=req.project,
            dry_run=req.dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not req.dry_run:
        logger.warning(
            f"ESTATE TRANSFER {req.from_principal} -> {req.to_principal} "
            f"ns={req.namespace} project={req.project} rows={rows}"
        )
        await audit("admin.estate_transfer", _caller, {
            "from_principal": req.from_principal,
            "to_principal": req.to_principal,
            "namespace": req.namespace, "project": req.project,
            "rows": rows,
        })
    return EstateTransferResponse(
        status="ok",
        from_principal=req.from_principal,
        to_principal=req.to_principal,
        rows=rows,
        dry_run=req.dry_run,
        guidance=(
            f"DRY RUN — {rows} row(s) would change custodian; re-send with "
            f'"dry_run": false to execute.'
            if req.dry_run else
            f"{rows} row(s) now carry custodian='{req.to_principal}'. "
            f"owner (authorship) is unchanged on every one."
        ),
    )


@router.get("/deletion-queue", response_model=DeletionQueueResponse)
async def deletion_queue_endpoint(
    limit: int = Query(200, ge=1, le=1000),
    _caller=Depends(admin_or_open),
):
    """MEM-8: rows flagged for physical destruction, awaiting review.

    Execute one by deleting it via /memory/forget (admin bypasses the
    controller gate); decline one via /admin/deletion-queue/reject, which
    restores its prior lifecycle status.
    """
    items = await deletion_queue_list(limit=limit)
    return DeletionQueueResponse(
        status="ok", items=[DeletionQueueItem(**i) for i in items]
    )


@router.post("/deletion-queue/reject", response_model=DeletionRejectResponse)
async def deletion_queue_reject_endpoint(
    req: DeletionRejectRequest,
    _caller=Depends(admin_or_open),
):
    try:
        row = await deletion_queue_reject(
            namespace=req.namespace,
            key=req.key,
            scope=req.scope,
            user_id=req.user_id,
            project=req.project,
            actor_principal=(_caller or {}).get("name"),
            reason=req.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no deletion-flagged row '{req.key}' at that partition",
        )
    await audit("admin.deletion_queue.reject", _caller, {
        "namespace": req.namespace, "key": req.key, "scope": req.scope,
        "user_id": req.user_id, "project": req.project, "reason": req.reason,
    })
    return DeletionRejectResponse(status="ok", key=req.key)


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
        raise HTTPException(status_code=500, detail="internal error — see server logs")
