"""运行期配置，全部来自环境变量（12-factor）。"""
from __future__ import annotations

import os
from functools import lru_cache


def _database_url() -> str:
    # Compose 中直接注入 DATABASE_URL；本地单跑时可用各分项拼装。
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "expo")
    password = os.getenv("POSTGRES_PASSWORD", "expo")
    db = os.getenv("POSTGRES_DB", "expo")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


class Settings:
    def __init__(self) -> None:
        self.database_url: str = _database_url()
        self.host: str = os.getenv("APP_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("APP_PORT", "8080"))
        self.db_min_size: int = int(os.getenv("DB_POOL_MIN", "2"))
        self.db_max_size: int = int(os.getenv("DB_POOL_MAX", "10"))
        self.db_connect_timeout: float = float(os.getenv("DB_CONNECT_TIMEOUT", "10"))
        # 等待数据库起来的总时长（秒），覆盖 compose 首次启动 PG 初始化窗口。
        self.db_startup_wait: float = float(os.getenv("DB_STARTUP_WAIT", "60"))
        # 单批最大上报条数，防超大事务。
        self.max_batch_size: int = int(os.getenv("MAX_BATCH_SIZE", "2000"))
        # 咨询锁的固定命名空间（int4），避免与库内其它用途冲突。
        self.lock_namespace: int = int(os.getenv("LOCK_NAMESPACE", "88001"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
