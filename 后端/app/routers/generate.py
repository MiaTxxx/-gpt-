"""图片生成路由。

提供同步和异步两种接口：
  - POST /api/generate         同步：等图生成出来再返回，超过约 90s 会被反代切断
  - POST /api/generate/async   异步：立刻返回 task_id，前端轮询 GET 看结果
  - GET  /api/generate/task/{id}   查询任务状态
  - POST /api/upload           上传参考图

底层都走 gpt_pool.image_generation.generate_one 调 ChatGPT。

错误码（同步接口）：
    429  用户额度已用完
    503  账号池为空或所有账号不可用（PoolExhaustedError）
    502  远程报错重试用尽（GenerationFailedError）
    504  超时（asyncio.TimeoutError）
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.dependencies import get_current_user
from app.models.conversation import Conversation
from app.models.generated_image import GeneratedImage
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import (
    GenerateRequest,
    GenerateResponse,
    GenerateTaskCreate,
    GenerateTaskStatus,
)
from app.services.gpt_pool import GENERATION_TIMEOUT_SECONDS
from app.services.gpt_pool.image_generation import (
    GenerationFailedError,
    PoolExhaustedError,
    generate_one,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generate"])

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ============================================================================
# 文件上传
# ============================================================================

async def save_file(file: UploadFile) -> str:
    ext = (
        file.filename.rsplit(".", 1)[-1].lower()
        if file.filename and "." in file.filename
        else ""
    )
    if ext not in ("jpg", "jpeg", "png", "webp", "gif", "bmp"):
        raise HTTPException(status_code=400, detail="不支持的文件格式")
    filename = f"{uuid.uuid4().hex}_{file.filename}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    return f"/uploads/{filename}"


@router.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    file_url = await save_file(file)
    return {"file_url": file_url}


# ============================================================================
# aspect_ratio → size 映射
# ============================================================================

def _aspect_to_size(aspect_ratio: str | None) -> str | None:
    if not aspect_ratio or aspect_ratio == "auto":
        return None
    mapping = {
        "1:1": "1024x1024",
        "16:9": "1536x1024",
        "9:16": "1024x1536",
        "4:3": "1024x768",
    }
    return mapping.get(aspect_ratio)


# ============================================================================
# 同步生图
# ============================================================================

async def _resolve_or_create_conversation(
    db: AsyncSession,
    data: GenerateRequest,
    current_user: User,
) -> Conversation:
    if data.conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == data.conversation_id,
                Conversation.user_id == current_user.id,
            )
        )
        conv = result.scalar_one_or_none()
        if not conv:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在"
            )
    else:
        conv = Conversation(
            user_id=current_user.id,
            title=data.prompt[:30] + ("..." if len(data.prompt) > 30 else ""),
            is_private=bool(data.is_private) if data.is_private is not None else False,
        )
        db.add(conv)
        await db.flush()

    if data.is_private is not None and bool(conv.is_private) != bool(data.is_private):
        conv.is_private = bool(data.is_private)

    return conv


async def _persist_generation(
    db: AsyncSession,
    *,
    user: User,
    conv: Conversation,
    prompt: str,
    aspect_ratio: str,
    image_url: str,
) -> GeneratedImage:
    user_msg = Message(conversation_id=conv.id, role="user", content=prompt)
    db.add(user_msg)
    assistant_msg = Message(conversation_id=conv.id, role="assistant", content=image_url)
    db.add(assistant_msg)
    img = GeneratedImage(
        user_id=user.id,
        conversation_id=conv.id,
        prompt=prompt,
        image_url=image_url,
        aspect_ratio=aspect_ratio,
        is_private=conv.is_private,
    )
    db.add(img)
    user.quota_remaining = max(0, int(user.quota_remaining or 0) - 1)
    await db.commit()
    await db.refresh(img)
    return img


@router.post("/api/generate", response_model=GenerateResponse)
async def generate_image(
    data: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.quota_remaining <= 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="额度已用完"
        )

    conv = await _resolve_or_create_conversation(db, data, current_user)

    try:
        result = await asyncio.wait_for(
            run_in_threadpool(
                generate_one,
                data.prompt,
                n=1,
                size=_aspect_to_size(data.aspect_ratio),
            ),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("image generation timeout for user_id=%s", current_user.id)
        raise HTTPException(
            status_code=504,
            detail="图片生成超时，请改用异步接口 /api/generate/async（前端会自动切换）",
        )
    except PoolExhaustedError:
        logger.warning("pool exhausted for user_id=%s", current_user.id)
        raise HTTPException(status_code=503, detail="图片生成服务暂时不可用，请稍后再试")
    except GenerationFailedError as exc:
        logger.warning("generation failed for user_id=%s: %s", current_user.id, exc)
        raise HTTPException(status_code=502, detail="图片生成失败，请稍后重试")
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected generate error: %s", exc)
        raise HTTPException(status_code=502, detail="图片生成失败，请稍后重试")

    try:
        img = await _persist_generation(
            db,
            user=current_user,
            conv=conv,
            prompt=data.prompt,
            aspect_ratio=data.aspect_ratio,
            image_url=result.image_url,
        )
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "site db commit failed after successful generation, user_id=%s",
            current_user.id,
        )
        return GenerateResponse(
            id=-1,
            image_url=result.image_url,
            thumbnail_url=None,
            prompt=data.prompt,
            aspect_ratio=data.aspect_ratio,
            created_at=datetime.utcnow(),
        )

    return GenerateResponse(
        id=img.id,
        conversation_id=img.conversation_id,
        image_url=img.image_url,
        thumbnail_url=img.thumbnail_url,
        prompt=img.prompt,
        aspect_ratio=img.aspect_ratio,
        is_private=bool(img.is_private),
        created_at=img.created_at,
    )


# ============================================================================
# 异步生图：避开 Cloudflare 100s 上限
# ============================================================================

# 内存里的任务表。重启即丢失（这是开发态可接受的折中）。
# 生产建议换 Redis 或 DB 存。
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_TASK_TTL_SECONDS = 30 * 60  # 30 分钟后清理
_MAX_ASYNC_TIMEOUT = 180     # 异步任务最长等多久（含重试）


def _gc_tasks() -> None:
    now = time.time()
    with _TASKS_LOCK:
        stale = [k for k, v in _TASKS.items() if now - v.get("created_ts", now) > _TASK_TTL_SECONDS]
        for k in stale:
            _TASKS.pop(k, None)


def _task_to_status(task: dict[str, Any]) -> GenerateTaskStatus:
    image: GenerateResponse | None = None
    img = task.get("image")
    if img:
        image = GenerateResponse(**img)
    return GenerateTaskStatus(
        task_id=task["task_id"],
        status=task["status"],
        image=image,
        error=task.get("error"),
        started_at=task.get("started_at"),
        finished_at=task.get("finished_at"),
    )


async def _async_run(task_id: str, user_id: int, conv_id: int, prompt: str, aspect_ratio: str, size: str | None) -> None:
    """后台协程：跑生图 + 持久化。"""
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if t is None:
            return
        t["status"] = "running"
        t["started_at"] = datetime.now(timezone.utc)

    try:
        # 真生图（线程池 + 超时）
        result = await asyncio.wait_for(
            run_in_threadpool(generate_one, prompt, n=1, size=size),
            timeout=_MAX_ASYNC_TIMEOUT,
        )

        # 自己开一个 db session 做持久化（不能用请求里的 db，那个早被关了）
        async with async_session() as session:
            user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
            conv = (await session.execute(select(Conversation).where(Conversation.id == conv_id))).scalar_one()
            img = await _persist_generation(
                session, user=user, conv=conv, prompt=prompt, aspect_ratio=aspect_ratio, image_url=result.image_url
            )
            payload = GenerateResponse(
                id=img.id,
                conversation_id=img.conversation_id,
                image_url=img.image_url,
                thumbnail_url=img.thumbnail_url,
                prompt=img.prompt,
                aspect_ratio=img.aspect_ratio,
                is_private=bool(img.is_private),
                created_at=img.created_at,
            ).model_dump(mode="json")

        with _TASKS_LOCK:
            t = _TASKS.get(task_id)
            if t:
                t["status"] = "done"
                t["image"] = payload
                t["finished_at"] = datetime.now(timezone.utc)

    except asyncio.TimeoutError:
        _mark_task_failed(task_id, "图片生成超时")
    except PoolExhaustedError:
        _mark_task_failed(task_id, "账号池暂时不可用")
    except GenerationFailedError as exc:
        _mark_task_failed(task_id, f"生成失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("async generate failed task=%s", task_id)
        _mark_task_failed(task_id, f"内部错误：{exc}")


def _mark_task_failed(task_id: str, msg: str) -> None:
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
        if t:
            t["status"] = "failed"
            t["error"] = msg
            t["finished_at"] = datetime.now(timezone.utc)


@router.post("/api/generate/async", response_model=GenerateTaskCreate)
async def generate_image_async(
    data: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """提交异步生图。立刻返回 task_id，再轮询 GET /api/generate/task/{id}。"""
    if current_user.quota_remaining <= 0:
        raise HTTPException(status_code=429, detail="额度已用完")

    conv = await _resolve_or_create_conversation(db, data, current_user)
    await db.commit()  # 这里要把 conversation 落库，后台任务才能查到

    _gc_tasks()
    task_id = uuid.uuid4().hex
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "created_ts": time.time(),
            "user_id": current_user.id,
            "conv_id": conv.id,
            "prompt": data.prompt,
            "aspect_ratio": data.aspect_ratio,
        }

    asyncio.create_task(
        _async_run(
            task_id,
            current_user.id,
            conv.id,
            data.prompt,
            data.aspect_ratio,
            _aspect_to_size(data.aspect_ratio),
        )
    )
    return GenerateTaskCreate(task_id=task_id, status="pending")


@router.get("/api/generate/task/{task_id}", response_model=GenerateTaskStatus)
async def generate_task_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        if task.get("user_id") != current_user.id:
            raise HTTPException(status_code=403, detail="无权访问该任务")
        snapshot = dict(task)
    return _task_to_status(snapshot)
