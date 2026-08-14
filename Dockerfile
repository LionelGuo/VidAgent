# syntax=docker/dockerfile:1
# VidAgent 主逻辑镜像（FastAPI :8000 + Next.js 生产构建 :3000）
#
# 两种运行形态：
#   场景一（本地有 GPU）：本镜像 + 宿主 vLLM-omni 服务（见 scripts/deploy_vllm_omni.sh）
#   场景二（仅远程 API）：本镜像 + LLM_PROVIDER=siliconflow|generic + LLM_API_KEY
#
# 推荐以 --network=host 运行（CDP 平台复用宿主 Chrome :9222；浏览器直达 localhost）：
#   docker build -t vidagent .
#   docker run --network=host -e LLM_PROVIDER=siliconflow -e LLM_API_KEY=sk-xxx vidagent

# ── Stage 1：前端构建（standalone）──────────────────────────────────────────
FROM node:20-bookworm-slim AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2：运行时（Python + Node 最小运行时）──────────────────────────────
FROM python:3.11-slim AS runtime

# 系统依赖：ffmpeg（音视频处理）、git（MediaCrawler 等）、curl、
# libgl1/libglib2.0（playwright 运行时）、Node 22（yt-dlp JS runtime ≥22 + Next standalone server）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git curl ca-certificates \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv（快速 Python 包管理）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先装依赖（利用层缓存：仅 pyproject/uv.lock 变化才重装）
# README.md 也是 pyproject 的 hatchling readme 字段引用，构建包时需要
COPY pyproject.toml uv.lock README.md ./
# 安装运行所需 extras：server（FastAPI）+ douyin（playwright + MediaCrawler 依赖）
RUN uv sync --frozen --extra server --extra douyin

# 安装 Playwright 驱动（CDP 连接外部 Chrome，无需下载浏览器二进制）
RUN uv run playwright install-deps || true

# 前端 standalone 产物（自带最小 node_modules）
COPY --from=frontend-build /build/.next/standalone ./frontend/
COPY --from=frontend-build /build/.next/static ./frontend/.next/static
COPY --from=frontend-build /build/public ./frontend/public

# 后端代码
COPY src/ ./src/
COPY server/ ./server/
# MediaCrawler vendored 源码（抖音/小红书/快手 CDP 平台，详见 ADR-0007）
COPY vendor/ ./vendor/

# 运行时目录
RUN mkdir -p workspace logs

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_DIR=/app/workspace

EXPOSE 3000 8000

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
