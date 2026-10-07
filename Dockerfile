FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 启动时应用会自动等待数据库就绪并完成建表初始化
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
