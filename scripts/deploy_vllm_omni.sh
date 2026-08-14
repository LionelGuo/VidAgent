#!/usr/bin/env bash
# =============================================================================
# VidAgent 本地模型安装脚本 —— 安装 vllm-omni + 下载 Qwen3-Omni AWQ 模型
# =============================================================================
# 有 ≥24GB 显存（RTX 3090/4090/A100 等）可自托管模型，配合 VidAgent 主逻辑
# （Docker 镜像或裸机）作为「场景一」部署。安装完成后用 start_vllm_bare.sh 启动。
#
# 用法：
#   bash scripts/deploy_vllm_omni.sh [MODEL_DIR]
#   MODEL_DIR  模型本地目录（默认项目根目录 models/，与 start_vllm_bare.sh 一致）
#
# 可用环境变量覆盖（带默认值）：
#   MODEL_ID        模型 ID（默认 cpatonn/Qwen3-Omni-30B-A3B-Thinking-AWQ-4bit，
#                   第三方上传的公开 AWQ 4bit，约 18GB——Qwen 官方 HF 仓库 gated 需授权）
#   MODEL_SOURCE    下载源：hf-mirror（默认，国内可用，公开仓库免登录）
#                   / hf（官方 HF，gated 仓库需 HF_TOKEN + 同意 license）
#                   / modelscope（魔搭，需 MODEL_ID 指向存在的魔搭仓库）
# =============================================================================

set -euo pipefail

# 项目根目录：脚本位于 <repo>/scripts/，锚定脚本位置而非启动 CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

MODEL_ID="${MODEL_ID:-cpatonn/Qwen3-Omni-30B-A3B-Thinking-AWQ-4bit}"
MODEL_DIR="${1:-${REPO_ROOT}/models}"
MODEL_SOURCE="${MODEL_SOURCE:-hf-mirror}"

log() { echo -e "\033[36m[deploy_vllm]\033[0m $*"; }
err() { echo -e "\033[31m[deploy_vllm ERROR]\033[0m $*" >&2; }

check_gpu() {
    log "检查 GPU…"
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        err "未找到 nvidia-smi。需安装 NVIDIA 驱动 + CUDA。"
        exit 1
    fi
    local vram_mb
    vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
    log "GPU 显存：${vram_mb} MB"
    if [ "${vram_mb:-0}" -lt 23000 ]; then
        err "显存不足（${vram_mb}MB < 23000MB）。Qwen3-Omni AWQ 4bit 需 ≥24GB VRAM。"
        err "若显存不足，请改用「场景二」：Docker 主逻辑 + 远程 API key（LLM_PROVIDER=siliconflow）。"
        exit 1
    fi
}

check_gpu
log "安装 vllm-omni（需 Python 3.11 + PyTorch CUDA 构建）…"
pip install -U "vllm-omni>=0.18.0" || {
    err "vllm-omni 安装失败。请确认 Python 3.11 + CUDA 可用。"
    exit 1
}

if [ -f "${MODEL_DIR}/config.json" ]; then
    log "模型已存在（${MODEL_DIR}），跳过下载"
else
    log "下载模型（源=${MODEL_SOURCE}）→ ${MODEL_DIR} …"
    mkdir -p "$(dirname "${MODEL_DIR}")"
    if [ "${MODEL_SOURCE}" = "modelscope" ]; then
        pip install -U modelscope >/dev/null
        python -c "from modelscope import snapshot_download; snapshot_download('${MODEL_ID}', local_dir='${MODEL_DIR}')"
    elif [ "${MODEL_SOURCE}" = "hf" ]; then
        # 官方 HF：Qwen gated 仓库需先 HF_TOKEN=... huggingface-cli login 并同意 license
        pip install -U "huggingface_hub[cli]" >/dev/null
        huggingface-cli download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
    else
        # hf-mirror（默认）：国内镜像，公开仓库免登录
        pip install -U "huggingface_hub[cli]" >/dev/null
        HF_ENDPOINT=https://hf-mirror.com huggingface-cli download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
    fi
fi
log "✅ 安装完成。模型位于 ${MODEL_DIR}"
log "下一步：bash scripts/start_vllm_bare.sh 启动服务"
