"""诊断路由：管理员可调，用来确认容器到外部网络的连通性。

只暴露给管理员，避免泄露内网信息。
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from app.config import settings
from app.dependencies import admin_required
from app.models.user import User
from app.services.gpt_pool.config_adapter import config as pool_config

router = APIRouter(prefix="/api/admin/diag", tags=["admin-diag"])


def _tcp_check(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            return {"host": host, "port": port, "ok": True, "ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"host": host, "port": port, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _http_check(url: str, timeout: float = 8.0) -> dict[str, Any]:
    started = time.time()
    try:
        proxy = pool_config.get_proxy_settings() or None
        async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
            r = await client.get(url)
            return {"url": url, "ok": r.is_success, "status": r.status_code, "ms": int((time.time() - started) * 1000), "via_proxy": bool(proxy)}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}", "via_proxy": bool(pool_config.get_proxy_settings())}


@router.get("/network")
async def network_check(_: User = Depends(admin_required)) -> dict[str, Any]:
    """检查容器到外部的连通性。"""
    tcp_targets = [
        ("chat.openai.com", 443),
        ("auth.openai.com", 443),
        ("api.resend.com", 443),
        ("smtp.resend.com", 465),
    ]
    tcp_results = await asyncio.gather(
        *[asyncio.to_thread(_tcp_check, h, p) for h, p in tcp_targets]
    )
    http_results = await asyncio.gather(
        _http_check("https://chat.openai.com/"),
        _http_check("https://api.resend.com/"),
    )
    return {
        "proxy_in_use": pool_config.get_proxy_settings() or "(none)",
        "smtp_host": settings.SMTP_HOST,
        "tcp": list(tcp_results),
        "http": list(http_results),
    }
