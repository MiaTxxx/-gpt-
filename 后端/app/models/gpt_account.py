"""GptAccount: 账号池中的单个 ChatGPT 账号。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GptAccount(Base):
    __tablename__ = "gpt_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 业务 key：完整 access_token，唯一索引；返回前端时必须脱敏
    access_token: Mapped[str] = mapped_column(String(2048), unique=True, index=True, nullable=False)

    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="free")
    # 状态: 正常 / 限流 / 禁用 / 异常
    status: Mapped[str] = mapped_column(String(16), default="正常", index=True)
    quota: Mapped[int] = mapped_column(Integer, default=0)
    image_quota_unknown: Mapped[bool] = mapped_column(Boolean, default=False)

    # ISO 字符串，沿用项目 B 的格式以减少跨项目转换开销
    restore_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    success: Mapped[int] = mapped_column(Integer, default=0)
    fail: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    default_model_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_id_remote: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 远程接口返回的限制进度（list[dict]），SQLite 下走 TEXT 列自动 json 序列化
    limits_progress: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
