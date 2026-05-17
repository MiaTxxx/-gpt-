"""邮箱验证码服务（内存版）。

为什么用内存：当前后端是单进程开发模式，简单可靠。生产 / 多进程
建议换 Redis（接口完全一致：set/get/delete/has）。
"""
import asyncio
import secrets
import time
from dataclasses import dataclass

CODE_TTL_SECONDS = 10 * 60     # 10 分钟有效
RESEND_COOLDOWN_SECONDS = 60   # 60 秒冷却
MAX_ATTEMPTS = 5               # 5 次错误后码失效


@dataclass
class _Entry:
    code: str
    expires_at: float
    attempts: int = 0


class VerificationStore:
    def __init__(self) -> None:
        self._codes: dict[str, _Entry] = {}
        self._last_sent_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def can_send(self, email: str) -> tuple[bool, int]:
        """是否可以再发一次；返回 (可发?, 还需等待秒)。"""
        async with self._lock:
            last = self._last_sent_at.get(email, 0.0)
            elapsed = time.time() - last
            if elapsed < RESEND_COOLDOWN_SECONDS:
                return False, int(RESEND_COOLDOWN_SECONDS - elapsed)
            return True, 0

    async def issue(self, email: str) -> str:
        """生成并保存一条验证码。"""
        code = f"{secrets.randbelow(1_000_000):06d}"
        async with self._lock:
            self._codes[email] = _Entry(code=code, expires_at=time.time() + CODE_TTL_SECONDS)
            self._last_sent_at[email] = time.time()
        return code

    async def verify(self, email: str, code: str) -> tuple[bool, str]:
        """校验验证码；返回 (通过?, 失败原因)。
        通过后立即作废（一次性）。"""
        async with self._lock:
            entry = self._codes.get(email)
            if not entry:
                return False, "请先获取验证码"
            if time.time() > entry.expires_at:
                self._codes.pop(email, None)
                return False, "验证码已过期，请重新获取"
            if entry.attempts >= MAX_ATTEMPTS:
                self._codes.pop(email, None)
                return False, "尝试次数过多，请重新获取验证码"

            if not secrets.compare_digest(entry.code, code.strip()):
                entry.attempts += 1
                left = MAX_ATTEMPTS - entry.attempts
                if left <= 0:
                    self._codes.pop(email, None)
                    return False, "验证码错误，已达上限，请重新获取"
                return False, f"验证码错误，还可尝试 {left} 次"

            # 通过，立即作废
            self._codes.pop(email, None)
            self._last_sent_at.pop(email, None)
            return True, ""

    async def gc(self) -> None:
        """清理过期项。"""
        async with self._lock:
            now = time.time()
            for k in [k for k, v in self._codes.items() if v.expires_at < now]:
                self._codes.pop(k, None)


store = VerificationStore()
