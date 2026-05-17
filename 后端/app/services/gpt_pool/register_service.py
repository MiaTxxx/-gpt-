"""注册机服务（SQLAlchemy 版）。

差别于项目 B 的实现：
    - JSON 配置文件被 RegisterConfig（单行 id=1）+ RegisterLog 表替代
    - 日志通过 SSE 推送，asyncio.Queue 作为线程↔事件循环之间的桥梁
    - 注册成功后通过 `account_service.add_accounts([token])` 入池（与项目 B 一致）

公共方法：get / update / start / stop / reset / shutdown / event_stream
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import desc, select

from app.models.register_config import RegisterConfig, RegisterLog
from app.services.gpt_pool.account_service import account_service
from app.services.gpt_pool.db_session import SyncSessionLocal

logger = logging.getLogger(__name__)


VALID_MODES = {"total", "quota", "available"}

DEFAULT_MAIL_CONFIG = {
    "request_timeout": 15,
    "wait_timeout": 30,
    "wait_interval": 0.8,
    "providers": [],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_row_to_dict(row: RegisterConfig) -> dict:
    mail = row.mail_config if isinstance(row.mail_config, dict) else None
    if not mail:
        mail = dict(DEFAULT_MAIL_CONFIG)
    # 保险：保证关键键存在
    mail.setdefault("request_timeout", 15)
    mail.setdefault("wait_timeout", 30)
    mail.setdefault("wait_interval", 0.8)
    mail.setdefault("providers", [])
    return {
        "mail": mail,
        "proxy": row.proxy or "",
        "total": int(row.total or 1),
        "threads": int(row.threads or 1),
        "mode": row.mode or "total",
        "target_quota": int(row.target_quota or 1),
        "target_available": int(row.target_available or 1),
        "check_interval": int(row.check_interval or 5),
        "enabled": bool(row.enabled),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _log_row_to_dict(row: RegisterLog) -> dict:
    return {
        "id": row.id,
        "time": row.time.isoformat() if row.time else None,
        "level": row.level or "info",
        "text": row.text or "",
        "job_id": row.job_id,
    }


class RegisterService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        # 事件订阅者：每个 SSE 连接一个 asyncio.Queue
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        # 任务级 stats
        self._stats: dict = {
            "job_id": None,
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "started_at": None,
            "updated_at": None,
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
        }

    # ========================================================================
    # 启动 / 关闭
    # ========================================================================
    def startup(self) -> None:
        """lifespan 启动时调用：确保 RegisterConfig 单行存在 + 默认 mail_config。"""
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            if row is None:
                row = RegisterConfig(id=1)
                session.add(row)
                session.commit()
                row = session.get(RegisterConfig, 1)
            if not row.mail_config:
                row.mail_config = {
                    **DEFAULT_MAIL_CONFIG,
                    "providers": [
                        {
                            "type": "tempmail_lol",
                            "enable": True,
                            "api_key": "",
                            "domain": [],
                        }
                    ],
                }
                session.commit()

        # 安装 log sink 到 openai_register（注入到模块级钩子）
        try:
            from app.services.gpt_pool import openai_register as oreg

            oreg.register_log_sink = self._append_log
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not install register log sink: %s", exc)

        # 如果上次 enabled=True，自动恢复
        cfg = self.get()
        if cfg.get("enabled"):
            self.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._set_enabled(False)
        if self._runner and self._runner.is_alive():
            self._runner.join(timeout=timeout)

    # ========================================================================
    # 配置 CRUD
    # ========================================================================
    def get(self) -> dict:
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            if row is None:
                row = RegisterConfig(id=1, mail_config=dict(DEFAULT_MAIL_CONFIG))
                session.add(row)
                session.commit()
                row = session.get(RegisterConfig, 1)
            cfg = _config_row_to_dict(row)
            log_rows = (
                session.execute(
                    select(RegisterLog).order_by(desc(RegisterLog.time)).limit(300)
                )
                .scalars()
                .all()
            )
            # chatgpt2api 原版 logs 字段直接放在配置对象里（升序还是降序？看看原版 reverse() 在前端做）
            logs = [_log_row_to_dict(r) for r in log_rows]
            logs.reverse()  # 时间升序

        # 与 chatgpt2api 一致：把 stats 和 logs 都揉进 config 对象一起返回
        with self._lock:
            stats = dict(self._stats)
        stats.update(self._pool_metrics())
        cfg["stats"] = stats
        cfg["logs"] = logs
        return cfg

    def update(self, updates: dict) -> dict:
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            if row is None:
                row = RegisterConfig(id=1, mail_config=dict(DEFAULT_MAIL_CONFIG))
                session.add(row)

            # ===== mail（整块覆盖） =====
            if "mail" in updates and isinstance(updates["mail"], dict):
                m = dict(updates["mail"])
                # 强校验数值字段
                for key, default in (
                    ("request_timeout", 15),
                    ("wait_timeout", 30),
                ):
                    try:
                        m[key] = int(m.get(key, default))
                    except Exception:
                        m[key] = default
                try:
                    m["wait_interval"] = float(m.get("wait_interval", 0.8))
                except Exception:
                    m["wait_interval"] = 0.8
                providers = m.get("providers")
                if not isinstance(providers, list):
                    providers = []
                # 每个 provider 至少要有 type 和 enable 两个字段
                clean_providers = []
                for p in providers:
                    if not isinstance(p, dict):
                        continue
                    pp = dict(p)
                    pp["type"] = str(pp.get("type") or "tempmail_lol")
                    pp["enable"] = bool(pp.get("enable", True))
                    clean_providers.append(pp)
                m["providers"] = clean_providers
                row.mail_config = m

            if "proxy" in updates:
                row.proxy = str(updates["proxy"] or "").strip()
            if "total" in updates and updates["total"] is not None:
                row.total = max(1, int(updates["total"]))
            if "threads" in updates and updates["threads"] is not None:
                row.threads = max(1, int(updates["threads"]))
            if "mode" in updates and updates["mode"] is not None:
                m_ = str(updates["mode"]).strip()
                if m_ in VALID_MODES:
                    row.mode = m_
            if "target_quota" in updates and updates["target_quota"] is not None:
                row.target_quota = max(1, int(updates["target_quota"]))
            if "target_available" in updates and updates["target_available"] is not None:
                row.target_available = max(1, int(updates["target_available"]))
            if "check_interval" in updates and updates["check_interval"] is not None:
                row.check_interval = max(1, int(updates["check_interval"]))
            if "enabled" in updates and updates["enabled"] is not None:
                row.enabled = bool(updates["enabled"])

            session.commit()
        return self.get()

    def reset(self) -> dict:
        with SyncSessionLocal() as session:
            session.query(RegisterLog).delete()
            session.commit()
        with self._lock:
            self._stats = {
                "job_id": None,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "started_at": None,
                "updated_at": _now_iso(),
                "elapsed_seconds": 0,
                "avg_seconds": 0,
                "success_rate": 0,
            }
        return self.get()

    def _set_enabled(self, value: bool) -> None:
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            if row is None:
                row = RegisterConfig(id=1, enabled=value)
                session.add(row)
            else:
                row.enabled = value
            session.commit()

    def _is_enabled(self) -> bool:
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            return bool(row and row.enabled)

    def _read_config(self) -> dict:
        """worker 用。返回需要的字段（含完整 mail config）。"""
        with SyncSessionLocal() as session:
            row = session.get(RegisterConfig, 1)
            if row is None:
                return {}
            mail = row.mail_config if isinstance(row.mail_config, dict) else None
            if not mail:
                mail = dict(DEFAULT_MAIL_CONFIG)
            return {
                "mail": mail,
                "proxy": row.proxy or "",
                "total": int(row.total or 1),
                "threads": int(row.threads or 1),
                "mode": row.mode or "total",
                "target_quota": int(row.target_quota or 1),
                "target_available": int(row.target_available or 1),
                "check_interval": int(row.check_interval or 5),
            }

    # ========================================================================
    # 启停
    # ========================================================================
    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._set_enabled(True)
                return self.get()
            self._set_enabled(True)
            cfg = self._read_config()
            self._stats = {
                "job_id": uuid.uuid4().hex,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "elapsed_seconds": 0,
                "avg_seconds": 0,
                "success_rate": 0,
                "threads": cfg.get("threads", 1),
            }
            # 把 site 配置注入到 openai_register 模块级 config（worker 会读它）
            try:
                from app.services.gpt_pool import openai_register as oreg

                oreg.config["mail"] = cfg.get("mail") or dict(DEFAULT_MAIL_CONFIG)
                oreg.config["proxy"] = cfg["proxy"]
                oreg.config["total"] = cfg["total"]
                oreg.config["threads"] = cfg["threads"]
                with oreg.stats_lock:
                    oreg.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            except Exception as exc:  # noqa: BLE001
                logger.warning("openai_register not ready: %s", exc)

            self._runner = threading.Thread(
                target=self._run, daemon=True, name="gpt-register"
            )
            self._runner.start()
            self._append_log(
                f"注册任务启动，模式={cfg.get('mode')}，线程数={cfg.get('threads')}",
                "yellow",
            )
        return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._set_enabled(False)
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
        return self.get()

    # ========================================================================
    # 日志 / SSE
    # ========================================================================
    def _broadcast_state(self) -> None:
        """不写日志，仅把当前完整 state 推送给所有 SSE 订阅者。"""
        try:
            payload = self.get()
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            for loop, queue in list(self._subscribers):
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
                except Exception:  # noqa: BLE001
                    pass

    def _append_log(self, text: str, color: str = "") -> None:
        """同时写库 + 推订阅者。可被任何线程调用。

        与 chatgpt2api 原版一致：每次日志触发，向所有 SSE 订阅者推送
        完整的当前 state（含 stats、logs[最新300条]）。前端直接 setState。
        """
        level = (color or "info").strip() or "info"
        try:
            with SyncSessionLocal() as session:
                row = RegisterLog(
                    level=level, text=str(text), job_id=self._stats.get("job_id")
                )
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("write register log failed: %s", exc)

        # 拼出整个 state，推给订阅者
        self._broadcast_state()

    async def event_stream(self) -> AsyncIterator[dict]:
        """SSE 异步生成器。每次 yield 一个完整 state dict。"""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append((loop, queue))
        try:
            # 连接刚建立时立刻发一次当前完整状态，前端不必等下一次日志
            yield self.get()
            while True:
                event = await queue.get()
                yield event
        finally:
            with self._lock:
                self._subscribers = [
                    (l, q) for (l, q) in self._subscribers if q is not queue
                ]

    # ========================================================================
    # 后台 worker
    # ========================================================================
    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [a for a in items if a.get("status") == "正常"]
        return {
            "current_quota": sum(
                int(a.get("quota") or 0)
                for a in normal
                if not a.get("image_quota_unknown")
            ),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = self._pool_metrics()
        with self._lock:
            self._stats.update(metrics)
            self._stats["updated_at"] = _now_iso()
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(
                f"检查号池：可用={metrics['current_available']}，剩余额度={metrics['current_quota']}，"
                f"目标={cfg.get('target_quota')}，{'跳过' if reached else '继续'}注册",
                "yellow",
            )
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(
                f"检查号池：可用={metrics['current_available']}，目标={cfg.get('target_available')}，"
                f"{'跳过' if reached else '继续'}注册",
                "yellow",
            )
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _compute_runtime_stats(self, done: int, success: int, fail: int, running: int) -> dict:
        """计算 elapsed/avg/success_rate（与 chatgpt2api 原版 _bump 一致）。"""
        out: dict = {"running": running, "done": done, "success": success, "fail": fail}
        started_at = str(self._stats.get("started_at") or "")
        if started_at:
            try:
                elapsed = max(
                    0.0,
                    (
                        datetime.now(timezone.utc) - datetime.fromisoformat(started_at)
                    ).total_seconds(),
                )
            except Exception:  # noqa: BLE001
                elapsed = 0.0
            out["elapsed_seconds"] = round(elapsed, 1)
            out["avg_seconds"] = round(elapsed / success, 1) if success else 0
        out["success_rate"] = round(success * 100 / max(1, success + fail), 1)
        out["updated_at"] = _now_iso()
        return out

    def _run(self) -> None:
        # 检查 Playwright 是否可用（worker 会用到）
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception:  # noqa: BLE001
            self._append_log(
                "注册机依赖未就绪：未检测到 Playwright；请运行 `playwright install chromium` 后重试",
                "error",
            )
            with self._lock:
                self._set_enabled(False)
            return

        try:
            from app.services.gpt_pool import openai_register as oreg
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"openai_register 模块加载失败：{exc}", "error")
            with self._lock:
                self._set_enabled(False)
            return

        cfg = self._read_config()
        threads = int(cfg.get("threads") or 1)
        submitted = done = success = fail = 0

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = set()
            while True:
                cfg = self._read_config()
                while (
                    self._is_enabled()
                    and not self._target_reached(cfg, submitted)
                    and len(futures) < threads
                ):
                    submitted += 1
                    futures.add(executor.submit(oreg.worker, submitted))

                with self._lock:
                    self._stats.update(
                        self._compute_runtime_stats(done, success, fail, len(futures))
                    )

                if not futures and (
                    not self._is_enabled() or str(cfg.get("mode") or "total") == "total"
                ):
                    break

                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue

                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for fut in finished:
                    done += 1
                    try:
                        result = fut.result()
                        ok = bool(result.get("ok")) if isinstance(result, dict) else False
                        # 注册成功 → 入池
                        if ok and isinstance(result, dict):
                            token = (
                                result.get("access_token")
                                or result.get("token")
                                or ""
                            )
                            if token:
                                try:
                                    account_service.add_accounts([token])
                                except Exception as exc:  # noqa: BLE001
                                    self._append_log(
                                        f"add_accounts 失败：{exc}", "error"
                                    )
                        if ok:
                            success += 1
                        else:
                            fail += 1
                    except Exception as exc:  # noqa: BLE001
                        fail += 1
                        self._append_log(f"worker 异常：{exc}", "error")

                # 一轮 future 收尾后立刻刷新 stats，让前端 SSE 收到最新 success/fail/avg
                with self._lock:
                    self._stats.update(
                        self._compute_runtime_stats(done, success, fail, len(futures))
                    )
                # 触发一次 SSE 推送（用一条空字符串日志承载完整 state；前端不展示）
                self._broadcast_state()

        with self._lock:
            self._stats.update(self._compute_runtime_stats(done, success, fail, 0))
            self._stats["finished_at"] = _now_iso()
            self._set_enabled(False)
        self._append_log(f"注册任务结束，成功 {success}，失败 {fail}", "yellow")


# 模块级单例
register_service = RegisterService()
