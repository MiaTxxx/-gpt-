from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./dev.db"
    SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    # 默认 7 天，避免用户刷新页面老被踹回登录页
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "http://localhost:8088/api/auth/github/callback"

    FRONTEND_URL: str = "http://localhost:5173"

    # ===== Email / SMTP =====
    REQUIRE_EMAIL_VERIFICATION: bool = True
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""           # 发件邮箱（一般和 SMTP_USER 一致）
    SMTP_FROM_NAME: str = "Txxx的公益站"
    SMTP_USE_SSL: bool = True     # 465 端口设 true
    SMTP_USE_TLS: bool = False    # 587 端口设 true（STARTTLS）

    # 走 Resend HTTPS API（443 端口），可绕过国内服务器封 SMTP 出网。
    # 留空则继续走 SMTP；填了就优先用 API。
    RESEND_API_KEY: str = ""

    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # ===== ChatGPT 账号池 / 生图 =====
    CHATGPT_BACKEND_BASE_URL: str = "https://chat.openai.com"
    CHATGPT_PROXY: str = ""  # 出向代理（http/https/socks5）；为空则直连
    IMAGE_GENERATION_TIMEOUT: int = 60
    MAX_GENERATION_RETRIES: int = 2
    POOL_MAX_INFLIGHT_PER_ACCOUNT: int = 1
    REGISTER_PLAYWRIGHT_HEADLESS: bool = True

    # ===== 站点公开 base URL（用于生图 URL 前缀） =====
    SITE_PUBLIC_BASE_URL: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
