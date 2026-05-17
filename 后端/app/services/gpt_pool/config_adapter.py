"""把项目 B 的 `services.config` 桥接到 site 的 settings。

项目 B 的代码大量使用 `from services.config import config` 形式访问配置。
为最小化拷贝来代码的修改量，这里提供一个对象字面相同名的 `config` 单例，
属性按需从 `app.config.settings` 读取或给一个合理默认值。

注意：site 中并不需要项目 B 的全部能力（图片下载、CPA、sub2api 等），
所以这里只补齐生图与注册机所需的最小子集，其它属性按访问报 AttributeError，
通过测试发现哪个属性真的被用到再补。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.config import settings


class _PoolConfig:
    """与项目 B `services.config.config` 对外契约相似的桥接对象。"""

    # ===== 来自 site .env 的字段 =====
    @property
    def auth_key(self) -> str:
        # site 体系里 admin 已有 JWT，不需要单独的 auth-key；保留接口避免 AttributeError
        return ""

    @property
    def image_account_concurrency(self) -> int:
        return int(getattr(settings, "POOL_MAX_INFLIGHT_PER_ACCOUNT", 1))

    @property
    def image_generation_timeout(self) -> int:
        return int(getattr(settings, "IMAGE_GENERATION_TIMEOUT", 60))

    @property
    def register_playwright_headless(self) -> bool:
        return bool(getattr(settings, "REGISTER_PLAYWRIGHT_HEADLESS", True))

    @property
    def chatgpt_backend_base_url(self) -> str:
        return getattr(settings, "CHATGPT_BACKEND_BASE_URL", "https://chat.openai.com")

    # ===== 项目 B 协议代码会读这些属性，缺一不可 =====

    @property
    def global_system_prompt(self) -> str:
        # site 不注入额外 system prompt
        return ""

    @property
    def base_url(self) -> str:
        """图片落地后给前端用的图片 URL 前缀。

        - 默认空串 → 生成的图片 URL 是相对路径 `/images/...`，
          浏览器会按当前页面的 origin 去取，配合反向代理（或后端同时托管前端）
          一律可用。
        - 想要绝对 URL 时，在 .env 里设 SITE_PUBLIC_BASE_URL=https://xxx 即可。
        """
        env_base = (getattr(settings, "SITE_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        return env_base

    @property
    def image_poll_timeout_secs(self) -> int:
        return 120

    @property
    def image_retention_days(self) -> int:
        return 30

    @property
    def sensitive_words(self) -> list[str]:
        return []

    @property
    def ai_review(self) -> dict:
        return {}

    @property
    def app_version(self) -> str:
        return "0.0.0"

    # ===== 自动清理策略，沿用项目 B 默认值 =====
    auto_remove_invalid_accounts: bool = True
    auto_remove_rate_limited_accounts: bool = False

    # ===== 路径相关 =====
    @property
    def data_dir(self) -> Path:
        d = Path("data")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def images_dir(self) -> Path:
        d = self.data_dir / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def image_thumbnails_dir(self) -> Path:
        d = self.data_dir / "image_thumbnails"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ===== 项目 B 部分代码会调用 =====
    def get_storage_backend(self) -> Any:
        """JSON 存储已被 SQLAlchemy 替代，禁止再返回任何 storage backend。"""
        raise RuntimeError("storage backend disabled in site integration; use SQLAlchemy session instead")

    def cleanup_old_images(self) -> None:
        """site 的图片管理走另一条路径，不做自动清理。"""
        return None

    # ===== log.py 用到 =====
    @property
    def log_levels(self) -> set[str]:
        return {"info", "yellow", "error", "warning", "debug"}

    def get_proxy_settings(self) -> str:
        """全局出向代理：优先用 settings.CHATGPT_PROXY，否则尝试用注册机配置。"""
        # 先看 .env
        env_proxy = (getattr(settings, "CHATGPT_PROXY", "") or "").strip()
        if env_proxy:
            return env_proxy
        # 再看 register_config 的 proxy 字段（与原 chatgpt2api 相同：注册机和上游共用一个代理）
        try:
            from app.services.gpt_pool.db_session import SyncSessionLocal
            from app.models.register_config import RegisterConfig

            with SyncSessionLocal() as s:
                row = s.get(RegisterConfig, 1)
                if row and row.proxy:
                    return str(row.proxy).strip()
        except Exception:
            pass
        return ""


# 项目 B 同名风格：模块级单例
config = _PoolConfig()


# 项目 B 部分模块也会 `from services.config import DATA_DIR`
DATA_DIR = config.data_dir


# ===== proxy_settings 简易代理对象（项目 B 的 services.proxy_service） =====
class _ProxySettings:
    """build_session_kwargs：把 config.get_proxy_settings() 注入到 curl_cffi session。"""

    def build_session_kwargs(self, **session_kwargs) -> dict:
        proxy = config.get_proxy_settings()
        if proxy:
            session_kwargs["proxy"] = proxy
        return session_kwargs


proxy_settings = _ProxySettings()
