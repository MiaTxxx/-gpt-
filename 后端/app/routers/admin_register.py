"""/api/admin/register* 路由：管理员注册机视图。

所有路由 Depends(admin_required)。
SSE 用 fastapi.responses.StreamingResponse 推送 register_service 的事件流。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.dependencies import admin_required
from app.models.user import User
from app.schemas.register import RegisterConfigUpdate
from app.services.gpt_pool.register_service import register_service

router = APIRouter(prefix="/api/admin/register", tags=["admin-register"])


@router.get("")
async def get_config(_: User = Depends(admin_required)):
    return await run_in_threadpool(register_service.get)


@router.post("")
async def update_config(
    body: RegisterConfigUpdate,
    _: User = Depends(admin_required),
):
    return await run_in_threadpool(
        register_service.update, body.model_dump(exclude_none=True)
    )


@router.post("/start")
async def start(_: User = Depends(admin_required)):
    return await run_in_threadpool(register_service.start)


@router.post("/stop")
async def stop(_: User = Depends(admin_required)):
    return await run_in_threadpool(register_service.stop)


@router.post("/reset")
async def reset(_: User = Depends(admin_required)):
    return await run_in_threadpool(register_service.reset)


@router.get("/events")
async def events(_: User = Depends(admin_required)):
    """SSE 事件流。前端 EventSource 消费。"""

    async def gen():
        async for event in register_service.event_stream():
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
