"""gpt_pool 内部使用的同步 SQLAlchemy session。

site 主体跑在 asyncio + AsyncSession（aiosqlite 驱动）上，但 chatgpt2api
移植过来的代码全部是同步的（threading + ThreadPoolExecutor + 阻塞 HTTP）。
为了不强行 async 化大量 worker 代码，这里给同一份 SQLite 文件再开一个
同步 engine，专供 gpt_pool 子包内部使用。

两个 engine 同时操作同一个 SQLite 文件是安全的，前提是开启 WAL 模式
（main.py 的 lifespan 里会执行 `PRAGMA journal_mode=WAL`）。
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings


def _to_sync_url(url: str) -> str:
    """把 aiosqlite/asyncpg 驱动名转成同步驱动名。"""
    if "+aiosqlite" in url:
        return url.replace("+aiosqlite", "", 1)
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+psycopg2", 1)
    if "+aiomysql" in url:
        return url.replace("+aiomysql", "+pymysql", 1)
    return url


SYNC_DATABASE_URL = _to_sync_url(settings.DATABASE_URL)

# check_same_thread=False：sessionmaker 会被多个线程共用
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if SYNC_DATABASE_URL.startswith("sqlite") else {},
)

SyncSessionLocal = sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False)
