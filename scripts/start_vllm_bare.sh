#!/bin/bash
# VidAgent vLLM bare mode 启动脚本（自托管场景，≥24GB VRAM）
# 关键：不传 --enable-auto-tool-choice 和 --tool-call-parser
# 让 Qwen3-Omni 自由输出文本（含 <tool_call> XML），由 SSE Relay 流式转换
#
# 用法：
#   MODEL_PATH=/path/to/Qwen3-Omni-Thinking-AWQ-4bit bash scripts/start_vllm_bare.sh
# 可选环境变量：
#   VLLM_BIN   默认 vllm-omni（PATH 上）
#   DATA_PATH  默认当前目录（--allowed-local-media-path，视频帧本地路径白名单）
#   PORT       默认 6006
#   LOG_FILE   默认 ./vllm-server.log
#
# VLLM_MAX_AUDIO_DECODE_DURATION_S=3600：允许最长 60 分钟音频输入（默认仅 600s）

set -euo pipefail

MODEL_PATH="${MODEL_PATH:?请设置 MODEL_PATH 指向模型目录（如 Qwen3-Omni-Thinking-AWQ-4bit）}"
VLLM_BIN="${VLLM_BIN:-vllm-omni}"
DATA_PATH="${DATA_PATH:-$(pwd)}"
PORT="${PORT:-6006}"
LOG_FILE="${LOG_FILE:-./vllm-server.log}"

export VLLM_MAX_AUDIO_DECODE_DURATION_S=3600

nohup "$VLLM_BIN" serve \
  "$MODEL_PATH" \
  --port "$PORT" \
  --gpu-memory-utilization 0.85 \
  --max-num-batched-tokens 49152 \
  --max-num-seqs 2 \
  --enable-prefix-caching \
  --allowed-local-media-path "$DATA_PATH" \
  --limit-mm-per-prompt '{"video": {"count": 1, "num_frames": 10, "width": 512, "height": 512}}' \
  > "$LOG_FILE" 2>&1 &

echo "vLLM starting... PID=$!"
echo "VLLM_MAX_AUDIO_DECODE_DURATION_S=$VLLM_MAX_AUDIO_DECODE_DURATION_S"
echo "Monitor: tail -f $LOG_FILE"
