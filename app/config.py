"""服务配置项（均可通过环境变量覆盖）。"""
import os

# PostgreSQL 连接串
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://gate:gate@localhost:5432/gate"
)

# 启动时等待数据库就绪的重试参数
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_DELAY = float(os.getenv("DB_CONNECT_DELAY", "1.0"))  # 秒

# 连接池
POOL_MIN_SIZE = int(os.getenv("POOL_MIN_SIZE", "1"))
POOL_MAX_SIZE = int(os.getenv("POOL_MAX_SIZE", "10"))

# 上报批次的瞬态冲突（唯一键竞争/死锁）自动重试次数
SUBMIT_RETRY_ATTEMPTS = int(os.getenv("SUBMIT_RETRY_ATTEMPTS", "3"))
