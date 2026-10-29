"""Thread management endpoints for muse.ai conversation threads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...services.container import Services
from ..deps import ACCOUNT_ROUTING_PARAMS, get_services, require_api_key
from ..schemas import ThreadRenameRequest

router = APIRouter(tags=["threads"], dependencies=[Depends(require_api_key)])


def _account_id(request: Request) -> str | None:
    return request.headers.get("x-account-id") or request.query_params.get("account_id")


def _thread_view(t: Any) -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "url": t.url,
            "created_at": t.created_at, "updated_at": t.updated_at}


@router.get("/v1/threads")
async def list_threads(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    svc: Services = Depends(get_services),
) -> dict:
    """List the account's muse.ai conversation threads, newest first.

    `thread_id`/`url` values plug straight into the `thread` field of
    `/v1/chat/completions`. Pagination is opaque: pass `next_cursor` from the
    previous response back as `cursor` until it comes back null.
    """
    threads, next_cursor = await svc.gateway.list_threads(
        limit, cursor, account_id=_account_id(request))
    return {"object": "list",
            "data": [_thread_view(t) for t in threads],
            "next_cursor": next_cursor}


@router.get("/v1/threads/{thread_id:path}/messages",
            openapi_extra={"parameters": ACCOUNT_ROUTING_PARAMS})
async def thread_messages(
    thread_id: str, request: Request, svc: Services = Depends(get_services)
) -> dict:
    """Transcript of one thread, oldest first.

    ``thread_id`` accepts the bare id or the full ``https://muse.ai/thread/<id>``
    URL (percent-encode it when it contains slashes). ``created_at`` is unix
    seconds when the page exposes a time, else null.
    """
    messages = await svc.gateway.thread_messages(thread_id, account_id=_account_id(request))
    return {"object": "list", "data": messages}


@router.patch(
    "/v1/threads/{thread_id:path}",
    openapi_extra={
        "parameters": ACCOUNT_ROUTING_PARAMS,
        "requestBody": {"content": {"application/json": {"examples": {
            "rename": {
                "summary": "Rename the thread",
                "value": {"title": "Tên mới cho hội thoại"},
            },
        }}}},
    },
)
async def rename_thread(
    thread_id: str, body: ThreadRenameRequest, request: Request,
    svc: Services = Depends(get_services),
) -> dict:
    """Rename a muse.ai thread. Returns the updated thread object."""
    thread = await svc.gateway.rename_thread(
        thread_id, body.title.strip(), account_id=_account_id(request))
    return _thread_view(thread)


@router.delete("/v1/threads/{thread_id:path}",
               openapi_extra={"parameters": ACCOUNT_ROUTING_PARAMS})
async def delete_thread(
    thread_id: str, request: Request, svc: Services = Depends(get_services)
) -> dict:
    """Permanently delete a thread on muse.ai. This cannot be undone."""
    await svc.gateway.delete_thread(thread_id, account_id=_account_id(request))
    return {"deleted": True, "id": thread_id}
