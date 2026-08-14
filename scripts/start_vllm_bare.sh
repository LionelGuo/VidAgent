#!/bin/bash
# VidAgent vLLM-omni 启动脚本（自托管场景，≥24GB VRAM）
# 与生产实例同款命令：不加 --omni（AWQ compressed-tensors 在 --omni 多阶段流水线下
# 加载失败，vllm-omni issue #5573；普通 serve 模式可加载量化模型且音频/帧多模态可用）。
#
# 用法：
#   bash scripts/start_vllm_bare.sh [MODEL_PATH]
#   MODEL_PATH  模型目录（默认项目根目录 models/，与 deploy_vllm_omni.sh 的安装位置一致）
# 可选环境变量：
#   VLLM_BIN     默认 vllm-omni（PATH 上）
#   DATA_PATH    默认模型目录的上级（--allowed-local-media-path，视频帧本地路径白名单；
#                AutoDL 习惯：模型在 <data>/models/ 下，允许访问 <data>/ 整体）
#   PORT         默认 6006
#   GPU_MEM_UTIL 默认 0.85
#   LOG_FILE     默认 ./vllm-omni.log
#
# 前台运行（Ctrl+C 停止），日志同时写入 LOG_FILE
#
# VLLM_MAX_AUDIO_DECODE_DURATION_S=3600：允许最长 60 分钟音频输入（默认仅 600s）

set -euo pipefail

# 项目根目录：脚本位于 <repo>/scripts/，锚定脚本位置而非启动 CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

MODEL_PATH="${1:-${REPO_ROOT}/models}"
VLLM_BIN="${VLLM_BIN:-vllm-omni}"
PORT="${PORT:-6006}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.85}"
LOG_FILE="${LOG_FILE:-./vllm-omni.log}"

if [ ! -f "${MODEL_PATH}/config.json" ]; then
    echo "错误：模型未就位：${MODEL_PATH}" >&2
    echo "请先运行 bash scripts/deploy_vllm_omni.sh 安装，或通过参数指定已装模型目录。" >&2
    exit 1
fi

if ! command -v "$VLLM_BIN" >/dev/null 2>&1; then
    echo "错误：未找到 ${VLLM_BIN}。请先运行 bash scripts/deploy_vllm_omni.sh 安装。" >&2
    exit 1
fi

# 模型目录归一化为绝对路径（相对路径锚定到启动 CWD）
MODEL_PATH="$(cd "$(dirname "${MODEL_PATH}")" && pwd)/$(basename "${MODEL_PATH}")"
# --allowed-local-media-path 默认取模型目录的上级
DATA_PATH="${DATA_PATH:-$(dirname "$(dirname "${MODEL_PATH}")")}"

export VLLM_MAX_AUDIO_DECODE_DURATION_S=3600

echo "对外端点：http://<本机IP>:${PORT}/v1（VidAgent 的 LLM_BASE_URL 指向此处）"
echo "前台运行中（Ctrl+C 停止），日志同时写入 ${LOG_FILE}"

"$VLLM_BIN" serve \
  "$MODEL_PATH" \
  --port "$PORT" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --max-num-batched-tokens 49152 \
  --max-num-seqs 2 \
  --enable-prefix-caching \
  --allowed-local-media-path "$DATA_PATH" \
  --limit-mm-per-prompt '{"video": {"count": 1, "num_frames": 10}}' \
  2>&1 | tee "$LOG_FILE"
