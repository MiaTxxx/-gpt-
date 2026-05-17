from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import Base, engine
from app.middleware.cors import setup_cors
from app.routers import (
    admin,
    admin_accounts,
    admin_register,
    auth,
    conversations,
    diag,
    feedback,
    gallery,
    generate,
    images,
    users,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 异步引擎建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 给 gpt_pool 用的同步引擎初始化（WAL 模式 + 触发表创建）
    try:
        from app.services.gpt_pool.db_session import sync_engine

        with sync_engine.connect() as c:
            c.execute(text("PRAGMA journal_mode=WAL"))
            c.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync_engine PRAGMA WAL failed: %s", exc)

    # 3. 单例服务启动
    try:
        from app.services.gpt_pool.account_service import account_service
        from app.services.gpt_pool.register_service import register_service

        account_service.startup()
        register_service.startup()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gpt_pool services startup failed: %s", exc)

    yield

    # 4. 优雅关闭
    try:
        from app.services.gpt_pool.register_service import register_service

        register_service.shutdown(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("register_service shutdown failed: %s", exc)
    await engine.dispose()


app = FastAPI(title="Txxx的公益站 API", version="0.2.0", lifespan=lifespan)

setup_cors(app)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(admin.router)
app.include_router(conversations.router)
app.include_router(images.router)
app.include_router(gallery.router)
app.include_router(generate.router)
app.include_router(feedback.router)
app.include_router(admin_accounts.router)
app.include_router(admin_register.router)
app.include_router(diag.router)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# 生图产物目录（data/images），由 chatgpt2api 移植代码写入
import os as _os
_os.makedirs("data/images", exist_ok=True)
app.mount("/images", StaticFiles(directory="data/images"), name="generated-images")


@app.get("/api/health")
async def health():
    return {"status": "ok"}
