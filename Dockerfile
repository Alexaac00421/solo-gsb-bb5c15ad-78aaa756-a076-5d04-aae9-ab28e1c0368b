FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db

EXPOSE 8080

# 服务启动时会等待数据库并幂等执行 db/schema.sql
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
