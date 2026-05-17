"""账号池相关 Pydantic schema。

注意：响应模型里**永远不会**出现 `access_token` 字段。完整 token 仅在
管理员调用 POST /api/admin/accounts 时入站，落库后通过 `masked_token`
脱敏返回。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.services.gpt_pool.helper import anonymize_token


def _mask(token: str | None) -> str:
    if not token:
        return ""
    return anonymize_token(token)


class GptAccountOut(BaseModel):
    id: int
    masked_token: str
    email: Optional[str] = None
    type: str = "free"
    status: str = "正常"
    quota: int = 0
    image_quota_unknown: bool = False
    success: int = 0
    fail: int = 0
    restore_at: Optional[str] = None
    last_used_at: Optional[str] = None
    default_model_slug: Optional[str] = None

    @classmethod
    def from_pool_dict(cls, item: dict) -> "GptAccountOut":
        return cls(
            id=int(item.get("id") or 0),
            masked_token=_mask(item.get("access_token")),
            email=item.get("email"),
            type=item.get("type") or "free",
            status=item.get("status") or "正常",
            quota=int(item.get("quota") or 0),
            image_quota_unknown=bool(item.get("image_quota_unknown")),
            success=int(item.get("success") or 0),
            fail=int(item.get("fail") or 0),
            restore_at=item.get("restore_at"),
            last_used_at=item.get("last_used_at"),
            default_model_slug=item.get("default_model_slug"),
        )


class AccountAddRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)

    @field_validator("tokens")
    @classmethod
    def strip_and_dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in v or []:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out


class AccountUpdateRequest(BaseModel):
    type: Optional[str] = None
    status: Optional[str] = None
    quota: Optional[int] = None
    image_quota_unknown: Optional[bool] = None
