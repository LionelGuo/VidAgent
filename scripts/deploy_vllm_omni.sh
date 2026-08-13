#!/usr/bin/env bash
# =============================================================================
# VidAgent 本地模型部署脚本 —— 自托管 vLLM-omni（Qwen3-Omni-Thinking）
# =============================================================================
# 在有足够显存的机器（≥24GB VRAM，如 RTX 3090/4090/A100）上本地运行模型，
# 配合 VidAgent 主逻辑（Docker 镜像或裸机）作为「场景一」部署。
#
# 硬件基线：≥24GB VRAM（AWQ 4bit 权重约 18GB + 推理开销）
# 软件：NVIDIA 驱动 + CUDA 12.x、Python 3.11、PyTorch（CUDA 构建）
#
# 用法：
#   bash scripts/deploy_vllm_omni.sh install   # 装 vllm-omni + 下载模型
#   bash scripts/deploy_vllm_omni.sh start      # 启动服务（前台日志，或加 --bg 后台）
#   bash scripts/deploy_vllm_omni.sh stop       # 停止服务
#
# 可用环境变量覆盖（带默认值）：
#   MODEL_ID        模型 ID（默认 Qwen/Qwen3-Omni-30B-A3B-Thinking，AWQ 4bit）
#   MODEL_DIR       模型本地目录（默认 ./models/Qwen3-Omni-Thinking）
#   MODEL_SOURCE    下载源：hf（HuggingFace，默认）/ modelscope（国内更快）
#   PORT            服务端口（默认 6006）
#   GPU_MEM_UTIL    显存利用率（默认 0.85）
# =============================================================================

set -euo pipefail

MODEL_ID="${MODEL_ID:-Qwen/Qwen3-Omni-30B-A3B-Thinking}"
MODEL_DIR="${MODEL_DIR:-$(pwd)/models/Qwen3-Omni-Thinking}"
MODEL_SOURCE="${MODEL_SOURCE:-hf}"
PORT="${PORT:-6006}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.85}"
LOG_FILE="${LOG_FILE:-./vllm-omni.log}"

# AutoDL / 常见部署：6006 是自定义服务端口，对外映射到 8443
# VidAgent 主逻辑把 OPENAI_BASE_URL 指向 http://<host>:${PORT}/v1

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

do_install() {
    check_gpu
    log "安装 vllm-omni（需 Python 3.11 + PyTorch CUDA 构建）…"
    # vllm-omni 是 vLLM 的 Qwen3-Omni 专用扩展（需 vLLM/VLLM-Omni ≥ 0.18.0，启动需 --omni 标志）
    pip install -U "vllm-omni>=0.18.0" || {
        err "vllm-omni 安装失败。请确认 Python 3.11 + CUDA 可用。"
        exit 1
    }

    log "下载模型（源=${MODEL_SOURCE}）→ ${MODEL_DIR} …"
    mkdir -p "$(dirname "${MODEL_DIR}")"
    if [ "${MODEL_SOURCE}" = "modelscope" ]; then
        pip install -U modelscope >/dev/null
        python -c "from modelscope import snapshot_download; snapshot_download('${MODEL_ID}', local_dir='${MODEL_DIR}')"
    else
        pip install -U huggingface_hub >/dev/null
        huggingface-cli download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
    fi
    log "✅ 安装完成。模型位于 ${MODEL_DIR}"
    log "下一步：bash scripts/deploy_vllm_omni.sh start"
}

do_start() {
    check_gpu
    if [ ! -d "${MODEL_DIR}" ]; then
        err "模型目录不存在：${MODEL_DIR}。请先运行 'install'。"
        exit 1
    fi

    # 允许最长 60 分钟音频输入（默认仅 600s）
    export VLLM_MAX_AUDIO_DECODE_DURATION_S=3600

    local BG=""; [[ "${1:-}" == "--bg" ]] && BG=1

    # 注意：vllm-omni 当前文档要求 --omni 标志启用 Qwen3-Omni 模式；
    # 但部分预装版本（如 AutoDL 镜像）的 vllm-omni 二进制已默认 omni、不认此标志。
    # 若启动报未知参数，删除下面的 --omni 即可（对齐 legacy scripts/start_vllm_bare.sh）。
    local cmd=(
        vllm-omni serve "${MODEL_DIR}"
        --omni
        --port "${PORT}"
        --gpu-memory-utilization "${GPU_MEM_UTIL}"
        --max-num-batched-tokens 49152
        --max-num-seqs 2
        --enable-prefix-caching
        --allowed-local-media-path "$(dirname "${MODEL_DIR}")"
        --limit-mm-per-prompt '{"video": {"count": 1, "num_frames": 10, "width": 512, "height": 512}}'
    )

    log "启动 vLLM-omni（端口 ${PORT}）…"
    log "模型：${MODEL_DIR}"
    if [ -n "${BG}" ]; then
        nohup "${cmd[@]}" > "${LOG_FILE}" 2>&1 &
        echo $! > vllm-omni.pid
        log "后台启动 PID=$(cat vllm-omni.pid)。日志：tail -f ${LOG_FILE}"
        log "对外端点：http://<本机IP>:${PORT}/v1（VidAgent 的 OPENAI_BASE_URL 指向此处）"
    else
        log "前台运行（Ctrl+C 停止）。日志同时写入 ${LOG_FILE}"
        exec "${cmd[@]}" 2>&1 | tee "${LOG_FILE}"
    fi
}

do_stop() {
    if [ -f vllm-omni.pid ]; then
        local pid; pid=$(cat vllm-omni.pid)
        kill "${pid}" 2>/dev/null && log "已停止 PID=${pid}" || log "PID=${pid} 未运行"
        rm -f vllm-omni.pid
    else
        err "未找到 vllm-omni.pid。可手动：pkill -f 'vllm-omni serve'"
        exit 1
    fi
}

case "${1:-}" in
    install) do_install ;;
    start)   shift; do_start "$@" ;;
    stop)    do_stop ;;
    *) echo "用法：bash scripts/deploy_vllm_omni.sh {install|start [--bg]|stop}"; exit 1 ;;
esac
