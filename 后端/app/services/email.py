"""邮件发送服务。

支持两条通道，优先级如下：
  1. Resend HTTP API（推荐）—— 走 443，绕过国内服务器常见的 25/465/587 SMTP 出网封锁
     启用条件：settings.RESEND_API_KEY 不为空，或 SMTP_HOST 是 resend 且 SMTP_PASSWORD 以 `re_` 开头
  2. SMTP（smtplib 同步阻塞，扔到线程池里跑）

外部只关心 send_email() 接口，由它选择走哪条。
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# Resend HTTP API
# ============================================================================

def _resend_api_key() -> str:
    """优先用 RESEND_API_KEY；没设就尝试用 SMTP_PASSWORD（Resend SMTP 时密码就是 API key）。"""
    key = (getattr(settings, "RESEND_API_KEY", "") or "").strip()
    if key:
        return key
    pwd = (settings.SMTP_PASSWORD or "").strip()
    if pwd.startswith("re_") and "resend" in (settings.SMTP_HOST or "").lower():
        return pwd
    return ""


async def _send_via_resend_http(to_email: str, subject: str, html: str, text: str, api_key: str) -> None:
    sender = settings.SMTP_FROM or settings.SMTP_USER
    from_header = formataddr((settings.SMTP_FROM_NAME, sender)) if settings.SMTP_FROM_NAME else sender
    payload = {
        "from": from_header,
        "to": [to_email],
        "subject": subject,
        "html": html,
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
        if resp.status_code >= 400:
            # Resend 的错误体是 {"name":"...","message":"..."}，带上来方便排错
            raise RuntimeError(f"Resend API {resp.status_code}: {resp.text}")


# ============================================================================
# SMTP（备用）
# ============================================================================

def _send_via_smtp(to_email: str, subject: str, html: str, text: str) -> None:
    if not settings.SMTP_HOST or not settings.SMTP_USER:
        raise RuntimeError("SMTP 未配置：请先设置 backend/.env 里的 SMTP_HOST / SMTP_USER 等")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM or settings.SMTP_USER))
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if settings.SMTP_USE_SSL:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT, context=context, timeout=20
        ) as server:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            if settings.SMTP_USE_TLS:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)


# ============================================================================
# 对外接口
# ============================================================================

async def send_email(to_email: str, subject: str, html: str, text: str) -> None:
    api_key = _resend_api_key()
    if api_key:
        try:
            await _send_via_resend_http(to_email, subject, html, text, api_key)
            return
        except Exception as exc:
            # HTTP API 失败时记一条，但仍按原异常抛给路由层
            logger.warning("resend http api failed, will not fallback to smtp: %s", exc)
            raise

    # 没配 API key → 走 SMTP（旧行为）
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send_via_smtp, to_email, subject, html, text)


def render_verification_email(code: str, brand: str = "Txxx的公益站") -> tuple[str, str]:
    """返回 (html, text) 双版本，提高送达率。"""
    text = (
        f"你好！\n\n"
        f"你的 {brand} 验证码是：{code}\n"
        f"验证码 10 分钟内有效，请勿告诉他人。\n\n"
        f"如果不是你本人操作，请忽略此邮件。"
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>邮箱验证</title></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:'Helvetica Neue',Helvetica,'PingFang SC','Microsoft YaHei',Arial,sans-serif;color:#303133;">
  <div style="max-width:520px;margin:40px auto;background:#fff;border-radius:14px;padding:36px 32px;box-shadow:0 2px 12px rgba(0,0,0,0.04)">
    <h1 style="margin:0 0 16px;font-size:22px">邮箱验证</h1>
    <p style="margin:0 0 20px;line-height:1.7;color:#4a5568">
      你好，欢迎来到 <strong>{brand}</strong>。<br/>
      请使用下面的验证码完成账号注册：
    </p>
    <div style="background:#f0f4f9;border-radius:10px;padding:18px;text-align:center;letter-spacing:8px;font-size:30px;font-weight:700;color:#1d2030;font-family:'Courier New',monospace">
      {code}
    </div>
    <p style="margin:20px 0 0;line-height:1.7;color:#94a1b2;font-size:13px">
      验证码 10 分钟内有效。如果不是你本人操作，请忽略此邮件。
    </p>
    <hr style="border:none;border-top:1px solid #ebeef5;margin:28px 0 16px"/>
    <p style="margin:0;font-size:12px;color:#b5bdcc;text-align:center">
      © {brand} · 此邮件由系统自动发送，请勿直接回复
    </p>
  </div>
</body></html>"""
    return html, text
