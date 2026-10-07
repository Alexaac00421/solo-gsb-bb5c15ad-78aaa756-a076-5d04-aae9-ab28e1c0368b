"""PostgreSQL 连接池与启动初始化。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger("expo.db")

_pool: ConnectionPool | None = None
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def _create_pool() -> ConnectionPool:
    settings = get_settings()
    return ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_min_size,
        max_size=settings.db_max_size,
        timeout=settings.db_connect_timeout,
        kwargs={"autocommit": False},
        open=False,
    )


def init_db() -> None:
    """等待数据库可用，建连接池并执行幂等的 schema 初始化。"""
    global _pool
    settings = get_settings()
    pool = _create_pool()

    deadline = time.monotonic() + settings.db_startup_wait
    last_error: Exception | None = None
    while True:
        try:
            pool.open()
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except Exception as exc:  # 数据库尚未就绪
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"数据库在 {settings.db_startup_wait:.0f}s 内不可用: {last_error}"
                ) from exc
            log.warning("等待数据库就绪: %s", exc)
            time.sleep(1.0)

    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with pool.connection() as conn:
        conn.execute(schema_sql)
        conn.commit()
    log.info("数据库初始化完成")
    _pool = pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("数据库尚未初始化")
    return _pool


def close_db() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
