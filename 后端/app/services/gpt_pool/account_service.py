"""账号池服务（SQLAlchemy 版）。

本模块在 site 体系下作为模块级单例 `account_service` 暴露，公共方法签名与
项目 B 原版保持一致，使得 chatgpt2api 移植过来的 worker / register / protocol
代码可以直接 `from app.services.gpt_pool.account_service import account_service`
然后调用而无需修改业务逻辑。

存储：
    所有变更都先写到 SQLite (`gpt_accounts` 表)，再同步到内存 cache `_accounts`。
    内存 cache 在启动时一次性 from-DB 加载，平时只服务 in-memory 的并发选号
    （`get_available_access_token` / `_image_inflight`）。

并发：
    `_lock`     线程锁，护住内存 cache 与 `_image_inflight`
    `_image_slot_condition`  并发槽 condition variable（项目 B 行为不变）
    `_image_inflight`  token -> 当前并行调用数
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Condition, Lock
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.gpt_account import GptAccount
from app.services.gpt_pool.config_adapter import config
from app.services.gpt_pool.db_session import SyncSessionLocal
from app.services.gpt_pool.helper import anonymize_token

logger = logging.getLogger(__name__)


# ===== 状态常量 =====
STATUS_OK = "正常"
STATUS_RATE_LIMITED = "限流"
STATUS_DISABLED = "禁用"
STATUS_ABNORMAL = "异常"
ALLOWED_STATUS = {STATUS_OK, STATUS_RATE_LIMITED, STATUS_DISABLED, STATUS_ABNORMAL}


def _row_to_dict(row: GptAccount) -> dict:
    """ORM 行 -> 与项目 B 内存表示一致的 dict。"""
    return {
        "id": row.id,
        "access_token": row.access_token,
        "email": row.email,
        "type": row.type or "free",
        "status": row.status or STATUS_OK,
        "quota": int(row.quota or 0),
        "image_quota_unknown": bool(row.image_quota_unknown),
        "restore_at": row.restore_at,
        "success": int(row.success or 0),
        "fail": int(row.fail or 0),
        "last_used_at": row.last_used_at,
        "default_model_slug": row.default_model_slug,
        "user_id": row.user_id_remote,
        "limits_progress": list(row.limits_progress or []),
    }


def _normalize_updates(updates: dict) -> dict:
    """收口字段类型，避免坏数据进库。"""
    out: dict[str, Any] = {}
    if "type" in updates:
        out["type"] = str(updates["type"] or "free")
    if "status" in updates and updates["status"] is not None:
        s = str(updates["status"]).strip()
        if s and s in ALLOWED_STATUS:
            out["status"] = s
    if "quota" in updates and updates["quota"] is not None:
        out["quota"] = max(0, int(updates["quota"]))
    if "image_quota_unknown" in updates:
        out["image_quota_unknown"] = bool(updates["image_quota_unknown"])
    if "restore_at" in updates:
        out["restore_at"] = updates["restore_at"] or None
    if "email" in updates:
        out["email"] = updates["email"] or None
    if "default_model_slug" in updates:
        out["default_model_slug"] = updates["default_model_slug"] or None
    if "user_id" in updates or "user_id_remote" in updates:
        # 支持远程接口返回 user_id 字段名，但落库到 user_id_remote
        out["user_id_remote"] = updates.get("user_id") or updates.get("user_id_remote") or None
    if "limits_progress" in updates:
        v = updates["limits_progress"]
        out["limits_progress"] = list(v) if isinstance(v, list) else []
    if "success" in updates and updates["success"] is not None:
        out["success"] = max(0, int(updates["success"]))
    if "fail" in updates and updates["fail"] is not None:
        out["fail"] = max(0, int(updates["fail"]))
    if "last_used_at" in updates:
        out["last_used_at"] = updates["last_used_at"] or None
    return out


class AccountService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        # token -> dict（与项目 B 一致）
        self._accounts: dict[str, dict] = {}
        # token -> 当前并发调用数
        self._image_inflight: dict[str, int] = {}
        self._loaded = False

    # ========================================================================
    # 启动 / cache 重载
    # ========================================================================
    def startup(self) -> None:
        """从数据库一次性加载所有账号到内存 cache。lifespan 启动时调用一次。"""
        with SyncSessionLocal() as session:
            rows = session.execute(select(GptAccount)).scalars().all()
            with self._lock:
                self._accounts = {r.access_token: _row_to_dict(r) for r in rows}
                self._loaded = True
        logger.info("AccountService startup: loaded %d accounts", len(self._accounts))

    def _reload_one(self, session: Session, token: str) -> dict | None:
        row = session.execute(
            select(GptAccount).where(GptAccount.access_token == token)
        ).scalar_one_or_none()
        if row is None:
            return None
        item = _row_to_dict(row)
        with self._lock:
            self._accounts[token] = item
        return item

    # ========================================================================
    # 仅判定可用性（in-memory）
    # ========================================================================
    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {STATUS_DISABLED, STATUS_RATE_LIMITED, STATUS_ABNORMAL}:
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    def _list_ready_candidate_tokens(self, excluded: set[str] | None = None) -> list[str]:
        excluded = excluded or set()
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
            and (token := item.get("access_token") or "")
            and token not in excluded
        ]

    def _list_available_candidate_tokens(self, excluded: set[str] | None = None) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    # ========================================================================
    # 选号入口（与项目 B 行为一致）
    # ========================================================================
    def _acquire_next_candidate_token(self, excluded: set[str] | None = None) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded):
                    raise RuntimeError("no available image quota")
                tokens = self._list_available_candidate_tokens(excluded)
                if tokens:
                    token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[token] = int(self._image_inflight.get(token, 0)) + 1
                    return token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            current = int(self._image_inflight.get(access_token, 0))
            if current <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(self) -> str:
        """阻塞直到拿到一个可用 token；用尽时抛 RuntimeError('no available image quota')。"""
        attempted: set[str] = set()
        while True:
            token = self._acquire_next_candidate_token(excluded=attempted)
            attempted.add(token)
            try:
                account = self.fetch_remote_info(token, "get_available_access_token")
            except Exception:
                self.release_image_slot(token)
                continue
            if self._is_image_account_available(account or {}):
                return token
            self.release_image_slot(token)

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        """文本接口选号（site 暂未启用文本生成，但保留方法以兼容拷贝来的代码）。"""
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {STATUS_DISABLED, STATUS_ABNORMAL}
                and (token := account.get("access_token") or "")
                and token not in excluded
            ]
            if not candidates:
                return ""
            token = candidates[self._index % len(candidates)]
            self._index += 1
            return token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        self._update_columns(
            access_token, {"last_used_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )

    # ========================================================================
    # CRUD
    # ========================================================================
    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts.keys())

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == STATUS_RATE_LIMITED
                and (token := item.get("access_token") or "")
            ]

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def get_account_by_id(self, account_id: int) -> dict | None:
        with SyncSessionLocal() as session:
            row = session.get(GptAccount, account_id)
            return _row_to_dict(row) if row else None

    def add_accounts(self, tokens: Iterable[str]) -> dict:
        tokens = list(dict.fromkeys(t.strip() for t in tokens if t and t.strip()))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        added = 0
        skipped = 0
        with SyncSessionLocal() as session:
            for token in tokens:
                existing = session.execute(
                    select(GptAccount).where(GptAccount.access_token == token)
                ).scalar_one_or_none()
                if existing is not None:
                    skipped += 1
                    continue
                row = GptAccount(
                    access_token=token,
                    email=None,
                    type="free",
                    status=STATUS_OK,
                    quota=0,
                    image_quota_unknown=False,
                    limits_progress=[],
                )
                session.add(row)
                added += 1
            session.commit()
            # cache 同步：完整 reload 一次最稳
            rows = session.execute(select(GptAccount)).scalars().all()
            with self._lock:
                self._accounts = {r.access_token: _row_to_dict(r) for r in rows}
        logger.info("AccountService.add_accounts: added=%d skipped=%d", added, skipped)
        return {"added": added, "skipped": skipped, "items": self.list_accounts()}

    def delete_accounts(self, tokens_or_ids: list) -> dict:
        """按 token 列表或 id 列表删除（混合也支持）。"""
        tokens: list[str] = []
        ids: list[int] = []
        for item in tokens_or_ids or []:
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, str) and item.strip():
                tokens.append(item.strip())

        removed = 0
        with SyncSessionLocal() as session:
            if tokens:
                for t in tokens:
                    row = session.execute(
                        select(GptAccount).where(GptAccount.access_token == t)
                    ).scalar_one_or_none()
                    if row is not None:
                        session.delete(row)
                        removed += 1
            if ids:
                for aid in ids:
                    row = session.get(GptAccount, aid)
                    if row is not None:
                        session.delete(row)
                        removed += 1
            session.commit()
            rows = session.execute(select(GptAccount)).scalars().all()
            with self._lock:
                self._accounts = {r.access_token: _row_to_dict(r) for r in rows}
                # 清理 inflight
                live = set(self._accounts.keys())
                self._image_inflight = {k: v for k, v in self._image_inflight.items() if k in live}
                self._index = self._index % len(self._accounts) if self._accounts else 0
        logger.info("AccountService.delete_accounts: removed=%d", removed)
        return {"removed": removed, "items": self.list_accounts()}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        norm = _normalize_updates(updates)
        if not norm:
            return self.get_account(access_token)

        with SyncSessionLocal() as session:
            row = session.execute(
                select(GptAccount).where(GptAccount.access_token == access_token)
            ).scalar_one_or_none()
            if row is None:
                return None
            for k, v in norm.items():
                setattr(row, k, v)
            # 限流账号自动移除策略
            if (
                row.status == STATUS_RATE_LIMITED
                and getattr(config, "auto_remove_rate_limited_accounts", False)
            ):
                session.delete(row)
                session.commit()
                with self._lock:
                    self._accounts.pop(access_token, None)
                logger.info("auto-removed rate-limited account %s", anonymize_token(access_token))
                return None
            session.commit()
            return self._reload_one(session, access_token)

    def _update_columns(self, access_token: str, updates: dict) -> None:
        """内部用：直接按列更新，跳过 _normalize_updates。"""
        with SyncSessionLocal() as session:
            row = session.execute(
                select(GptAccount).where(GptAccount.access_token == access_token)
            ).scalar_one_or_none()
            if row is None:
                return
            for k, v in updates.items():
                setattr(row, k, v)
            session.commit()
            self._reload_one(session, access_token)

    # ========================================================================
    # 远程刷新 / 调用结果回写
    # ========================================================================
    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict | None:
        if not access_token:
            raise ValueError("access_token is required")
        # 延迟 import 避免循环
        from app.services.gpt_pool.openai_backend_api import (
            InvalidAccessTokenError,
            OpenAIBackendAPI,
        )

        try:
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            self.remove_invalid_token(access_token, event)
            raise
        except Exception:
            raise
        return self.update_account(access_token, result or {})

    def refresh_accounts(self, access_tokens: list[str]) -> dict:
        access_tokens = list(dict.fromkeys(t for t in access_tokens if t))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors: list[dict] = []
        max_workers = min(10, len(access_tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_remote_info, t, "refresh_accounts"): t
                for t in access_tokens
            }
            for fut in as_completed(futures):
                try:
                    if fut.result() is not None:
                        refreshed += 1
                except Exception as exc:
                    errors.append(
                        {"token": anonymize_token(futures[fut]), "error": str(exc)}
                    )

        return {"refreshed": refreshed, "errors": errors, "items": self.list_accounts()}

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        """token 失效：根据策略移除或置异常。"""
        if not getattr(config, "auto_remove_invalid_accounts", True):
            self.update_account(access_token, {"status": STATUS_ABNORMAL, "quota": 0})
            return False
        result = self.delete_accounts([access_token])
        removed = bool(result.get("removed"))
        if removed:
            logger.info(
                "auto-removed invalid token %s (event=%s)",
                anonymize_token(access_token),
                event,
            )
        else:
            self.update_account(access_token, {"status": STATUS_ABNORMAL, "quota": 0})
        return removed

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        """生图调用结果回写：成功扣账号额度，失败 fail+1。释放并发槽。"""
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with SyncSessionLocal() as session:
            row = session.execute(
                select(GptAccount).where(GptAccount.access_token == access_token)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.last_used_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            unknown = bool(row.image_quota_unknown)
            if success:
                row.success = int(row.success or 0) + 1
                if not unknown:
                    row.quota = max(0, int(row.quota or 0) - 1)
                    if row.quota == 0:
                        row.status = STATUS_RATE_LIMITED
                    elif row.status == STATUS_RATE_LIMITED:
                        row.status = STATUS_OK
            else:
                row.fail = int(row.fail or 0) + 1
            # 自动剔除限流策略
            if (
                row.status == STATUS_RATE_LIMITED
                and getattr(config, "auto_remove_rate_limited_accounts", False)
            ):
                session.delete(row)
                session.commit()
                with self._lock:
                    self._accounts.pop(access_token, None)
                return None
            session.commit()
            return self._reload_one(session, access_token)

    # ========================================================================
    # 给管理员控制台用的便利方法
    # ========================================================================
    def stats(self) -> dict:
        """返回 (pool_total_quota, pool_available_accounts) 给 /api/admin/stats。"""
        with self._lock:
            total = sum(
                int(a.get("quota") or 0)
                for a in self._accounts.values()
                if a.get("status") == STATUS_OK and not bool(a.get("image_quota_unknown"))
            )
            available = sum(
                1 for a in self._accounts.values() if a.get("status") == STATUS_OK
            )
            return {"pool_total_quota": total, "pool_available_accounts": available}


# 模块级单例
account_service = AccountService()
