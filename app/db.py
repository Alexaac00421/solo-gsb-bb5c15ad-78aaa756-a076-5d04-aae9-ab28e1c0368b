"""数据库连接与启动初始化（建表 DDL 幂等，可重复执行）。"""
import time

import psycopg
from psycopg_pool import ConnectionPool

from . import config

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS credentials (
        credential_no TEXT PRIMARY KEY,
        valid_from    TIMESTAMPTZ NOT NULL,
        valid_to      TIMESTAMPTZ NOT NULL,
        max_entries   INTEGER NOT NULL CHECK (max_entries >= 0),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        terminal_id   TEXT NOT NULL,
        seq           BIGINT NOT NULL,
        credential_no TEXT NOT NULL,
        verified_at   TIMESTAMPTZ NOT NULL,
        status        TEXT NOT NULL DEFAULT 'INVALID',
        reason        TEXT,
        entry_no      INTEGER,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (terminal_id, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_credential
        ON records (credential_no, verified_at, terminal_id, seq)
    """,
]

# 应用级连接池；lifespan 中显式 open/close
pool = ConnectionPool(
    config.DATABASE_URL,
    open=False,
    min_size=config.POOL_MIN_SIZE,
    max_size=config.POOL_MAX_SIZE,
)


def init_db() -> None:
    """启动时等待数据库就绪并执行建表 DDL（幂等）。"""
    last_exc: Exception | None = None
    for _ in range(config.DB_CONNECT_RETRIES):
        try:
            with psycopg.connect(config.DATABASE_URL) as conn:
                for stmt in DDL_STATEMENTS:
                    conn.execute(stmt)
            return
        except Exception as exc:  # 数据库尚未就绪等瞬态错误
            last_exc = exc
            time.sleep(config.DB_CONNECT_DELAY)
    raise RuntimeError(f"数据库初始化失败（重试 {config.DB_CONNECT_RETRIES} 次）: {last_exc}")
