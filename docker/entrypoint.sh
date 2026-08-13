#!/usr/bin/env bash
# VidAgent 容器入口：启动 FastAPI（:8000）+ Next.js 生产服务（:3000）
# 后台启 uvicorn，前台跑 Next standalone server（SIGTERM 传播）

set -e

cd /app

# ── FastAPI 后端 ──
exec_uv="uv run uvicorn server.main:app --host 0.0.0.0 --port 8000"
echo "[entrypoint] 启动 FastAPI: $exec_uv"
$exec_uv &
BACKEND_PID=$!

# ── Next.js standalone server ──
# FASTAPI_URL 指向同容器的后端（route.ts 服务端代理用）
export FASTAPI_URL="${FASTAPI_URL:-http://127.0.0.1:8000}"
cd /app/frontend
echo "[entrypoint] 启动 Next.js: PORT=3000 FASTAPI_URL=$FASTAPI_URL"
export PORT=3000
export HOSTNAME=0.0.0.0
node server.js &
FRONTEND_PID=$!

# 任意子进程退出则整体退出
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM

wait -n $BACKEND_PID $FRONTEND_PID
EXIT_CODE=$?
echo "[entrypoint] 子进程退出 (code=$EXIT_CODE)，停止全部"
kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
exit $EXIT_CODE
