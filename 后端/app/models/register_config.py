"""RegisterConfig: 注册机的可调参数（单行 id=1）；RegisterLog: 任务日志。"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RegisterConfig(Base):
    """单行配置表，固定 id=1。"""

    __tablename__ = "register_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ===== 邮箱配置（与 chatgpt2api 对齐：providers 数组式） =====
    # 结构示例：
    # {
    #   "request_timeout": 15,
    #   "wait_timeout": 30,
    #   "wait_interval": 0.8,
    #   "providers": [
    #     {"type": "tempmail_lol", "enable": true, "api_key": "...", "domain": [...]},
    #     ...
    #   ]
    # }
    mail_config: Mapped[dict] = mapped_column(JSON, default=dict)

    # ===== 以下字段保留以兼容旧迁移，但不再使用（IMAP 模式不在原版方案里） =====
    mail_host: Mapped[str] = mapped_column(String(255), default="")
    mail_port: Mapped[int] = mapped_column(Integer, default=993)
    mail_user: Mapped[str] = mapped_column(String(320), default="")
    mail_password: Mapped[str] = mapped_column(String(255), default="")

    proxy: Mapped[str] = mapped_column(String(255), default="")
    total: Mapped[int] = mapped_column(Integer, default=10)
    threads: Mapped[int] = mapped_column(Integer, default=1)
    # mode: total / quota / available
    mode: Mapped[str] = mapped_column(String(16), default="total")
    target_quota: Mapped[int] = mapped_column(Integer, default=100)
    target_available: Mapped[int] = mapped_column(Integer, default=10)
    check_interval: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RegisterLog(Base):
    __tablename__ = "register_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # info / yellow / error
    level: Mapped[str] = mapped_column(String(16), default="info")
    text: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
