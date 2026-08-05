"""Tool 3: extract_and_summarize —— 音频提取 + ASR + LLM 总结。

- 抽音：ffmpeg 子进程（utils.audio）
- ASR：faster-whisper（ctranslate2，GPU 优先）
- 总结：OpenAI 兼容协议（httpx 直调），云端 DeepSeek / 本地 Ollama
- 降级（文档 §5.2）：无音频轨 / ASR 失败 → 仅用「标题+简介」总结，不崩溃
"""

from __future__ import annotations

import logging

import httpx

from vidagent.config import settings
from vidagent.utils import storage
from vidagent.utils.audio import extract_audio
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)


class _LiveASR:
    """ASR 实时进度：供 UI 在工具执行期间轮询、逐段显示转写。

    本地单用户场景；工具线程写、UI 协程读，GIL 下单属性读写原子，够用。
    """

    def __init__(self) -> None:
        self.active = False
        self.partial = ""

    def begin(self) -> None:
        self.active = True
        self.partial = ""

    def update(self, text: str) -> None:
        self.partial = text

    def reset(self) -> None:
        self.active = False
        self.partial = ""


_live = _LiveASR()


def live_partial() -> str:
    """当前转写文本（仅 ASR 进行中非空）。"""
    return _live.partial if _live.active else ""


def live_active() -> bool:
    return _live.active


_SUMMARY_SYS = (
    "你是一个专业的视频内容总结助手。根据提供的视频语音转写文本（及标题/简介），"
    "用中文输出结构化总结：\n"
    "1. **核心观点**（1-3 条，最关键的结论或主张）\n"
    "2. **主要内容梳理**（按逻辑分点，简明扼要）\n"
    "若转写文本为空或质量很差，仅依据标题与简介做力所能及的总结，"
    "并在开头注明「⚠️ 仅有元数据，总结基于标题/简介」。"
)


def extract_and_summarize(local_path: str, metadata: dict | None = None) -> str:
    """对本地视频抽取音频、语音转写并生成结构化中文总结（Markdown）。

    无音频轨时自动降级为仅依据元数据的总结（不报错）。

    Args:
        local_path: 本地视频文件路径（用 download_video 返回的 local_path）。
        metadata: 视频元数据，至少含 title 与 desc（来自 search_and_fetch_videos）。

    Returns:
        结构化 Markdown 总结（核心观点 + 主要内容梳理）。
    """
    metadata = metadata or {}
    video_id = metadata.get("video_id", "")
    cache_path = storage.transcript_path(video_id) if video_id else None

    transcript = ""
    if cache_path and cache_path.exists():
        # 转写缓存命中：跳过抽音 + ASR
        transcript = cache_path.read_text(encoding="utf-8").strip()
        logger.info("ASR 命中缓存(%s)，转写 %d 字", video_id, len(transcript))
    else:
        _live.begin()
        last_logged = 0

        def on_partial(p: str) -> None:
            _live.update(p)
            nonlocal last_logged
            if len(p) - last_logged >= 800:
                last_logged = len(p)
                logger.info("…ASR 进行中，已转 %d 字", len(p))

        try:
            with Timer("音频提取(ffmpeg)"):
                mp3 = extract_audio(local_path)
            with Timer("ASR 转写(流式)"):
                transcript, _lang = _transcribe(mp3, on_partial=on_partial)
            logger.info("ASR 完成，转写 %d 字", len(transcript))
            if transcript and cache_path:  # 仅成功转写才缓存（空/降级不缓存）
                cache_path.write_text(transcript, encoding="utf-8")
        except Exception as e:  # 抽音/ASR 失败 → 降级
            logger.warning("ASR 失败，走降级总结（仅元数据）: %s", e)
        finally:
            _live.reset()

    with Timer("LLM 总结"):
        return _summarize(transcript, metadata)


# Whisper 模型单例：进程内只加载一次，避免每次转写重复加载权重进显存
_WHISPER = None


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel

        device = _resolve_device()
        compute_type = "float16" if device == "cuda" else "int8"
        with Timer("加载 faster-whisper(仅首次)"):
            logger.info(
                "首次加载 faster-whisper(%s, device=%s, compute=%s)",
                settings.whisper_model, device, compute_type,
            )
            _WHISPER = WhisperModel(
                settings.whisper_model, device=device, compute_type=compute_type
            )
    return _WHISPER


def _transcribe(mp3_path, on_partial=None) -> tuple[str, str]:
    """faster-whisper 转写，返回 (text, language)。模型复用单例。

    on_partial: 可选回调，每解码出一段就以「累计文本」调用一次 → 流式产出。
    """
    model = _get_whisper()
    segments, info = model.transcribe(str(mp3_path), beam_size=5, vad_filter=True)
    cumul: list[str] = []
    for seg in segments:
        cumul.append(seg.text)
        if on_partial:
            on_partial("".join(cumul).strip())
    return "".join(cumul).strip(), info.language


def _resolve_device() -> str:
    """auto → 依据 ctranslate2 CUDA 计数选 cuda/cpu。"""
    if settings.asr_device != "auto":
        return settings.asr_device
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _summarize(transcript: str, metadata: dict) -> str:
    base_url, api_key, model = settings.active_llm()
    if settings.llm_provider == "cloud" and not settings.openai_api_key:
        raise RuntimeError(
            f"未配置云端 LLM API key：请在 .env 设置 OPENAI_API_KEY（推荐 DeepSeek）。"
            f" 转写文本({len(transcript)} 字)已就绪，配好 key 后重试即可。"
        )

    meta_block = ""
    if metadata:
        meta_block = f"【标题】{metadata.get('title', '')}\n【简介】{metadata.get('desc', '')}\n"
    user = f"{meta_block}\n【语音转写】\n{transcript or '(空)'}\n\n请输出结构化总结。"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYS},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    resp = httpx.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"LLM 调用失败 HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()["choices"][0]["message"]["content"]
