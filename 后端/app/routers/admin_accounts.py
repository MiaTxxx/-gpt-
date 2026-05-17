"""/api/admin/accounts* 路由：管理员账号池视图。

所有路由 Depends(admin_required)。完整 access_token 仅在 POST 入站时携带，
其他响应一律使用 GptAccountOut（包含 masked_token，不含 access_token）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from app.dependencies import admin_required
from app.models.user import User
from app.schemas.gpt_account import (
    AccountAddRequest,
    AccountUpdateRequest,
    GptAccountOut,
)
from app.services.gpt_pool.account_service import account_service

router = APIRouter(prefix="/api/admin/accounts", tags=["admin-accounts"])


@router.get("", response_model=list[GptAccountOut])
async def list_accounts(
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    _: User = Depends(admin_required),
):
    items = account_service.list_accounts()
    if status_filter:
        items = [i for i in items if i.get("status") == status_filter]
    if search:
        s = search.lower()
        items = [
            i
            for i in items
            if (i.get("email") or "").lower().find(s) >= 0
            or (i.get("access_token") or "").lower().find(s) >= 0
        ]
    items = items[skip : skip + limit]
    return [GptAccountOut.from_pool_dict(i) for i in items]


@router.post("", response_model=dict)
async def add_accounts(
    body: AccountAddRequest,
    _: User = Depends(admin_required),
):
    if not body.tokens:
        raise HTTPException(status_code=400, detail="tokens is required")
    # 写库（同步），跑在线程池里
    add_result = await run_in_threadpool(account_service.add_accounts, body.tokens)
    # 后台触发刷新；不 await 返回结果，避免阻塞响应过久
    await run_in_threadpool(account_service.refresh_accounts, body.tokens)
    items = account_service.list_accounts()
    return {
        "added": add_result.get("added", 0),
        "skipped": add_result.get("skipped", 0),
        "items": [GptAccountOut.from_pool_dict(i).model_dump() for i in items],
    }


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_by_id(
    account_id: int,
    _: User = Depends(admin_required),
):
    item = account_service.get_account_by_id(account_id)
    if item is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    await run_in_threadpool(account_service.delete_accounts, [account_id])


@router.post("/{account_id}/refresh", response_model=GptAccountOut)
async def refresh_one(
    account_id: int,
    _: User = Depends(admin_required),
):
    item = account_service.get_account_by_id(account_id)
    if item is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    token = item["access_token"]
    try:
        await run_in_threadpool(account_service.fetch_remote_info, token, "admin_refresh_one")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"刷新失败：{exc}") from exc
    refreshed = account_service.get_account(token)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="账号已被自动剔除")
    return GptAccountOut.from_pool_dict(refreshed)


@router.post("/refresh-all")
async def refresh_all(
    _: User = Depends(admin_required),
):
    tokens = account_service.list_tokens()
    if not tokens:
        return {"refreshed": 0, "errors": []}
    result = await run_in_threadpool(account_service.refresh_accounts, tokens)
    return {
        "refreshed": result.get("refreshed", 0),
        "errors": result.get("errors", []),
    }


@router.patch("/{account_id}", response_model=GptAccountOut)
async def update_account(
    account_id: int,
    body: AccountUpdateRequest,
    _: User = Depends(admin_required),
):
    item = account_service.get_account_by_id(account_id)
    if item is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    token = item["access_token"]
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="未传任何要更新的字段")
    if "status" in updates and updates["status"] not in {"正常", "限流", "禁用", "异常"}:
        raise HTTPException(status_code=400, detail="status 取值不合法")
    updated = await run_in_threadpool(account_service.update_account, token, updates)
    if updated is None:
        # 自动剔除
        raise HTTPException(status_code=404, detail="账号已被自动剔除")
    return GptAccountOut.from_pool_dict(updated)
