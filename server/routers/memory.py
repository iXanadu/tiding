import logging

from fastapi import APIRouter, HTTPException, Request

from server.dependencies import check_namespace_access, check_namespaces_access, get_current_principal
from server.models import (
    InboxAckRequest,
    InboxAckResponse,
    InboxBanner,
    InboxListRequest,
    InboxListResponse,
    InboxSendRequest,
    InboxSendResponse,
    MemoryForgetRequest,
    MemoryForgetResponse,
    MemoryGetRequest,
    MemoryGetResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemorySetRequest,
    MemorySetResponse,
)
from server.services.identity import validate_address, validate_listen_set
from server.services.inbox_guidance import (
    ack_guidance,
    archive_guidance,
    inbox_list_guidance,
    send_guidance,
)
from server.services.memory_service import (
    INBOX_NAMESPACE,
    inbox_ack,
    inbox_archive,
    inbox_banner,
    inbox_list,
    inbox_send,
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
    project = request.headers.get("x-engram-project")
    if project:
        metadata["project"] = project
    cwd = request.headers.get("x-engram-cwd")
    if cwd:
        metadata["cwd"] = cwd
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
        banner = None
        if req.listen_set:
            try:
                listen_set = validate_listen_set(req.listen_set)
            except ValueError:
                listen_set = []
            if listen_set:
                banner_dict = await inbox_banner(
                    listen_set=listen_set,
                    reader_identity=req.reader_identity,
                )
                if banner_dict:
                    banner = InboxBanner(**banner_dict)
        return MemorySetResponse(status="ok", key=key, inbox_banner=banner)
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
        banner = None
        if req.listen_set:
            try:
                listen_set = validate_listen_set(req.listen_set)
            except ValueError:
                listen_set = []
            if listen_set:
                banner_dict = await inbox_banner(
                    listen_set=listen_set,
                    reader_identity=req.reader_identity,
                )
                if banner_dict:
                    banner = InboxBanner(**banner_dict)
        return MemorySearchResponse(status="ok", results=results, inbox_banner=banner)
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


# --- Inbox endpoints ----------------------------------------------------

@router.post("/send", response_model=InboxSendResponse)
async def send_inbox(req: InboxSendRequest, request: Request):
    """Send an inbox message addressed to a project or machine."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
    try:
        to = validate_address(req.to)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        msg_id = await inbox_send(
            to=to,
            body=req.body,
            subject=req.subject,
            from_=req.from_,
            thread_id=req.thread_id,
        )
        return InboxSendResponse(
            status="ok",
            id=msg_id,
            guidance=send_guidance(to=to, reader_identity=req.from_),
        )
    except Exception as e:
        logger.exception("inbox_send failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inbox", response_model=InboxListResponse)
async def list_inbox(req: InboxListRequest, request: Request):
    """List inbox messages for the given listen_set."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "read")
    try:
        listen_set = validate_listen_set(req.listen_set)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        messages = await inbox_list(
            listen_set=listen_set,
            reader_identity=req.reader_identity,
            unread_only=req.unread_only,
            limit=req.limit,
        )
        return InboxListResponse(
            status="ok",
            messages=messages,
            guidance=inbox_list_guidance(
                reader_identity=req.reader_identity or "(unknown)",
                listen_set=listen_set,
                msg_count=len(messages),
            ),
        )
    except Exception as e:
        logger.exception("inbox_list failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inbox/{message_id:path}/ack", response_model=InboxAckResponse)
async def ack_inbox(message_id: str, req: InboxAckRequest, request: Request):
    """Mark an inbox message as read by a specific reader."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
    try:
        updated = await inbox_ack(message_id=message_id, reader_identity=req.reader_identity)
        if not updated:
            raise HTTPException(status_code=404, detail=f"Inbox message {message_id!r} not found")
        return InboxAckResponse(status="ok", id=message_id, guidance=ack_guidance())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("inbox_ack failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inbox/{message_id:path}/archive", response_model=InboxAckResponse)
async def archive_inbox(message_id: str, req: InboxAckRequest, request: Request):
    """Archive an inbox message (hides from all inbox queries)."""
    check_namespace_access(get_current_principal(request), INBOX_NAMESPACE, "write")
    try:
        updated = await inbox_archive(
            message_id=message_id,
            reader_identity=req.reader_identity,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"Inbox message {message_id!r} not found")
        return InboxAckResponse(status="ok", id=message_id, guidance=archive_guidance())
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("inbox_archive failed")
        raise HTTPException(status_code=500, detail=str(e))
