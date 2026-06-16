# smart-crawler 部署镜像 —— FastAPI + 采集器 + 看板
FROM node:20-alpine AS frontend-build

WORKDIR /web
RUN corepack enable && corepack prepare pnpm@10.27.0 --activate
COPY frontend-app/package.json frontend-app/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend-app/ ./
RUN pnpm build

FROM node:20-alpine AS admin-build

WORKDIR /web
RUN corepack enable && corepack prepare pnpm@10.27.0 --activate
COPY admin-app/package.json admin-app/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY admin-app/ ./
RUN pnpm build

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖（curl_cffi 需 libssl/ca；Flexispot 等需 Playwright Chromium）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

# Playwright Chromium —— Flexispot 等 React SPA 站点采集所需
RUN playwright install --with-deps chromium

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY --from=frontend-build /web/dist ./frontend-app/dist
COPY --from=admin-build /web/dist ./admin-app/dist

WORKDIR /app/backend
EXPOSE 8077

# 数据卷：SQLite 持久化到 /app/data
VOLUME ["/app/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8077"]
