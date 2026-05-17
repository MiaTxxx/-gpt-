"""注册机相关 Pydantic schema。

与 chatgpt2api 原版接口对齐：mail 是嵌套 JSON（含 providers 数组），不再是 IMAP 字段。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class RegisterConfigUpdate(BaseModel):
    """patch 语义：只有传了的字段才覆盖。"""

    mail: Optional[dict[str, Any]] = None
    proxy: Optional[str] = None
    total: Optional[int] = None
    threads: Optional[int] = None
    mode: Optional[Literal["total", "quota", "available"]] = None
    target_quota: Optional[int] = None
    target_available: Optional[int] = None
    check_interval: Optional[int] = None
