import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    OAuthRequest,
    SendCodeRequest,
    SendCodeResponse,
    TokenResponse,
)
from app.schemas.user import UserCreate, UserResponse
from app.services.auth import create_access_token, hash_password, verify_password
from app.services.email import render_verification_email, send_email
from app.services.oauth import exchange_github_code, exchange_google_code
from app.services.verification import store as verification_store

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ===== 邮箱验证码 =====

@router.post("/send-code", response_model=SendCodeResponse)
async def send_verification_code(data: SendCodeRequest, db: AsyncSession = Depends(get_db)):
    """发送邮箱验证码（用于注册）。"""
    if not settings.REQUIRE_EMAIL_VERIFICATION:
        raise HTTPException(status_code=400, detail="当前未启用邮箱验证")

    if not settings.SMTP_HOST or not settings.SMTP_USER:
        raise HTTPException(status_code=500, detail="邮件服务尚未配置，请联系管理员")

    # 已注册的邮箱不再发码（防枚举：要不要返回 200 视产品策略而定，这里直接报错以提升用户感知）
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该邮箱已被注册")

    can, wait = await verification_store.can_send(data.email)
    if not can:
        raise HTTPException(status_code=429, detail=f"操作过于频繁，请 {wait} 秒后再试")

    code = await verification_store.issue(data.email)
    html, text = render_verification_email(code)

    try:
        await send_email(data.email, "【Txxx的公益站】邮箱验证码", html, text)
    except Exception as e:  # noqa: BLE001
        # 发送失败时回滚，避免占用冷却期
        raise HTTPException(status_code=500, detail=f"邮件发送失败：{e}") from e

    return SendCodeResponse(sent=True, cooldown_seconds=60)


@router.post("/register", response_model=TokenResponse)
async def register(data: UserCreate, db: AsyncSession = Depends(get_db)):
    if settings.REQUIRE_EMAIL_VERIFICATION:
        if not data.code:
            raise HTTPException(status_code=400, detail="请填写邮箱验证码")
        ok, reason = await verification_store.verify(data.email, data.code)
        if not ok:
            raise HTTPException(status_code=400, detail=reason)

    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已被注册")

    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(access_token=token, expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用")

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(access_token=token, expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


# ===== Google OAuth (前端拿 code，POST 给后端) =====

@router.post("/oauth/google", response_model=TokenResponse)
async def oauth_google(data: OAuthRequest, db: AsyncSession = Depends(get_db)):
    try:
        profile = await exchange_google_code(data.code)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google 认证失败")

    return await _oauth_upsert(db, profile)


# ===== GitHub OAuth (服务器端流程：后端发起 → GitHub 回调后端 → 后端重定向到前端) =====

def _build_state() -> str:
    """签发一个短期 state，用于防 CSRF。"""
    payload = {
        "nonce": secrets.token_urlsafe(16),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _verify_state(state: str) -> bool:
    try:
        jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return True
    except JWTError:
        return False


@router.get("/github/login")
async def github_login():
    """点击「GitHub 登录」时跳到这里，再由这里 302 到 GitHub 授权页。"""
    if not settings.GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth 未配置")

    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": _build_state(),
        "allow_signup": "true",
    }
    url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"
    return RedirectResponse(url, status_code=302)


@router.get("/github/callback")
async def github_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """GitHub 把用户带回到这里，附带 code。我们换取 token、建/找用户、签发 JWT，
    最后把 JWT 通过 URL fragment 带回到前端。"""
    frontend_cb = f"{settings.FRONTEND_URL}/oauth/callback"

    if error:
        return RedirectResponse(
            f"{frontend_cb}#error={error}&error_description={error_description or ''}",
            status_code=302,
        )

    if not code or not state or not _verify_state(state):
        return RedirectResponse(f"{frontend_cb}#error=invalid_state", status_code=302)

    try:
        profile = await exchange_github_code(code)
    except Exception:
        return RedirectResponse(f"{frontend_cb}#error=github_exchange_failed", status_code=302)

    token_resp = await _oauth_upsert(db, profile)
    # token 放在 fragment 里，浏览器历史记录能看到，但不会发到任何服务器
    return RedirectResponse(
        f"{frontend_cb}#token={token_resp.access_token}&expires_in={token_resp.expires_in}",
        status_code=302,
    )


# 兼容旧前端：保留 POST 形式（前端拿 code，POST 给后端）
@router.post("/oauth/github", response_model=TokenResponse)
async def oauth_github(data: OAuthRequest, db: AsyncSession = Depends(get_db)):
    try:
        profile = await exchange_github_code(data.code)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GitHub 认证失败")

    return await _oauth_upsert(db, profile)


async def _oauth_upsert(db: AsyncSession, profile: dict) -> TokenResponse:
    # Try to find existing user by OAuth provider + id
    result = await db.execute(
        select(User).where(
            User.oauth_provider == profile["provider"],
            User.oauth_id == profile["oauth_id"],
        )
    )
    user = result.scalar_one_or_none()

    if user:
        user.avatar_url = profile.get("avatar_url") or user.avatar_url
        user.full_name = profile.get("full_name") or user.full_name
        await db.commit()
        await db.refresh(user)
    else:
        # Check if email already exists; link accounts
        if profile.get("email"):
            result = await db.execute(select(User).where(User.email == profile["email"]))
            user = result.scalar_one_or_none()
            if user:
                user.oauth_provider = profile["provider"]
                user.oauth_id = profile["oauth_id"]
                user.avatar_url = profile.get("avatar_url") or user.avatar_url
                await db.commit()
                await db.refresh(user)

        if not user:
            user = User(
                email=profile.get("email", f"{profile['provider']}_{profile['oauth_id']}@oauth.user"),
                full_name=profile.get("full_name"),
                avatar_url=profile.get("avatar_url"),
                oauth_provider=profile["provider"],
                oauth_id=profile["oauth_id"],
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(access_token=token, expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
