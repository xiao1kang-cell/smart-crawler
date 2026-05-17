# smart-crawler 部署镜像 —— FastAPI + 采集器 + 看板
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖（curl_cffi 需 libssl；采集主力为 curl_cffi，不装 Playwright 浏览器以保持镜像小）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend
EXPOSE 8077

# 数据卷：SQLite 持久化到 /app/data
VOLUME ["/app/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8077"]
