#!/bin/bash
# VidAgent vLLM bare mode 启动脚本
# 关键：不传 --enable-auto-tool-choice 和 --tool-call-parser
# 让 Qwen3-Omni 自由输出文本（含 <tool_call> XML），由 SSE Relay 流式转换
#
# VLLM_MAX_AUDIO_DECODE_DURATION_S=3600：允许最长 60 分钟音频输入（默认仅 600s）

export VLLM_MAX_AUDIO_DECODE_DURATION_S=3600

nohup /root/miniconda3/bin/vllm-omni serve \
  /root/autodl-tmp/Qwen3-Omni-30B-AWQ \
  --port 6006 \
  --gpu-memory-utilization 0.85 \
  --max-num-batched-tokens 16384 \
  --max-num-seqs 8 \
  --enable-prefix-caching \
  > /root/autodl-tmp/server.log 2>&1 &

echo "vLLM starting... PID=$!"
echo "VLLM_MAX_AUDIO_DECODE_DURATION_S=$VLLM_MAX_AUDIO_DECODE_DURATION_S"
echo "Monitor: tail -f /root/autodl-tmp/server.log"
