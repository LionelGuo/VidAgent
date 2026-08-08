"""Tool 3: extract_and_summarize —— 音频提取 + ASR + LLM 总结。

- 抽音：ffmpeg 子进程（utils.audio）
- ASR：faster-whisper（ctranslate2，GPU 优先）
- 总结：OpenAI 兼容协议（httpx 直调），云端 DeepSeek / 本地 Ollama
- 降级（文档 §5.2）：无音频轨 / ASR 失败 → 仅用「标题+简介」总结，不崩溃
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

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


class _LiveSummary:
    """多模态总结实时流：UI 轮询时获取当前已生成的总结文本。

    与 _LiveASR 同样的单用户 GIL 设计。
    支持两种模式：
    - streaming: 逐 token 追加（短音频单请求）
    - chunked: 逐段追加完整摘要（长音频分块）
    """

    def __init__(self) -> None:
        self.active = False
        self.partial = ""

    def begin(self, label: str = "🎵 多模态总结中…") -> None:
        self.active = True
        self.partial = label + "\n\n"

    def append(self, text: str) -> None:
        self.partial += text

    def set(self, text: str) -> None:
        """替换全部（用于 chunk 进度更新）。"""
        self.partial = text

    def reset(self) -> None:
        self.active = False
        self.partial = ""


_live = _LiveASR()
_live_summary = _LiveSummary()


def live_partial() -> str:
    """当前转写文本（仅 ASR 进行中非空）。"""
    return _live.partial if _live.active else ""


def live_active() -> bool:
    return _live.active


def live_summary() -> str:
    """当前多模态总结流文本（模型输出中 / 分块进度中）。"""
    return _live_summary.partial if _live_summary.active else ""


def live_summary_active() -> bool:
    return _live_summary.active


_SUMMARY_SYS = (
    "你是一个专业的视频内容总结助手。根据提供的视频语音转写文本（及标题/简介），"
    "用中文输出结构化总结：\n"
    "1. **核心观点**（1-3 条，最关键的结论或主张）\n"
    "2. **主要内容梳理**（按逻辑分点，简明扼要）\n"
    "若转写文本为空或质量很差，仅依据标题与简介做力所能及的总结，"
    "并在开头注明「⚠️ 仅有元数据，总结基于标题/简介」。"
)

_SUMMARY_SYS_MULTIMODAL = (
    "你是一个专业的视频内容总结助手。你会收到视频的音频和关键帧画面，"
    "请直接聆听音频、观察画面，然后用中文输出结构化总结：\n"
    "1. **核心观点**（1-3 条，最关键的结论或主张）\n"
    "2. **主要内容梳理**（按逻辑分点，简明扼要）\n"
    "3. **关键帧画面描述**（简要描述各帧的视觉内容）\n"
    "请优先基于音频内容进行总结（音频通常包含主要信息），"
    "关键帧作为视觉补充。即使画面质量有限，只要音频可辨识，"
    "就应基于音频产出完整总结。"
)

_CHUNK_SUMMARY_SYS = (
    "你是一个视频片段总结助手。你会收到视频某一段落的音频和关键帧，"
    "请用中文输出该段落的简要总结（2-5 句话），聚焦关键信息和事件。"
)

_MERGE_SYS = (
    "你是一个视频总结助手。请将以下各段落的摘要合并为完整的结构化总结：\n"
    "1. **核心观点**（1-3 条）\n"
    "2. **主要内容梳理**（按时间线或逻辑分点）\n"
    "3. **关键帧画面描述**\n"
    "请确保覆盖视频全貌，不要遗漏重要信息。"
)

# 长音频分块阈值：超过此时长按段落分片处理（每段独立请求 + 最终合并）
_MAX_AUDIO_CHUNK_SECONDS = 3600  # 60 分钟


def _chat_completion(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
) -> str:
    """非流式 chat completion，返回完整响应文本。"""
    resp = httpx.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 调用失败 HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def _chat_completion_stream(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
) -> str:
    """流式 chat completion，返回完整响应文本；同时更新 _live_summary。"""
    payload = {**payload, "stream": True}
    accumulated = ""
    with httpx.stream(
        "POST", f"{base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 流式调用失败 HTTP {resp.status_code}: {resp.text[:300]}")
        for line in resp.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    token = delta.get("content", "")
                    if token:
                        accumulated += token
                        _live_summary.append(token)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
    return accumulated


def extract_and_summarize(local_path: str, metadata: dict | None = None) -> str:
    """对本地视频生成结构化中文总结（Markdown）。

    多模态模型（LLM_MULTIMODAL=true）：抽取音频 → 直送 LLM，跳过 ASR。
    普通模型：抽取音频 → ASR 转写 → 文本总结。

    无音频轨时自动降级为仅依据元数据的总结（不报错）。

    Args:
        local_path: 本地视频文件路径（用 download_video 返回的 local_path）。
        metadata: 视频元数据，至少含 title 与 desc。

    Returns:
        结构化 Markdown 总结（核心观点 + 主要内容梳理）。
    """
    metadata = metadata or {}
    video_id = metadata.get("video_id", "")

    # ── 多模态路径：音频直送 LLM，跳过 ASR ──
    if settings.llm_multimodal:
        _live.begin()
        _live.update("🎵 多模态模型分析音频中…")
        _live_summary.begin()
        try:
            with Timer("音频提取(ffmpeg)"):
                mp3 = extract_audio(local_path)
            with Timer("多模态总结(音频直送)"):
                return _summarize_multimodal(Path(mp3), metadata, video_path=Path(local_path))
        except Exception as e:
            logger.warning("多模态总结失败，走降级总结（仅元数据）: %s", e)
            with Timer("LLM 总结(降级)"):
                return _summarize("", metadata)
        finally:
            _live.reset()
            _live_summary.reset()

    # ── 原 ASR 路径 ──
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


def _get_audio_duration(mp3_path: Path) -> float:
    """ffprobe 获取音频时长（秒），失败返回 0。"""
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(mp3_path)],
        capture_output=True, text=True, timeout=10,
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        logger.warning("无法获取音频时长: %s", mp3_path)
        return 0.0


def _split_audio(mp3_path: Path, chunk_s: int, work_dir: Path) -> list[Path]:
    """将 mp3 按 chunk_s 秒切分为多个片段，返回按时间排序的路径列表。"""
    import subprocess
    duration = _get_audio_duration(mp3_path)
    if duration <= 0:
        return [mp3_path]

    chunks: list[Path] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = mp3_path.stem

    start = 0.0
    i = 0
    while start < duration - 1:  # 留 1s 余量，避免浮点边界空段
        i += 1
        end = min(start + chunk_s, duration)
        out_path = work_dir / f"{stem}_chunk{i:03d}.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start),
             "-i", str(mp3_path), "-c:a", "libmp3lame", "-q:a", "4",
             str(out_path)],
            capture_output=True, timeout=30,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            chunks.append(out_path)
            logger.debug("音频分块 [%d]: %.0fs–%.0fs → %s", i, start, end, out_path.name)
        start = end

    return chunks if chunks else [mp3_path]


def _summarize_chunk(
    chunk_mp3: Path, chunk_index: int, total_chunks: int,
    time_start: float, time_end: float,
    metadata: dict, frames: list[Path],
    base_url: str, api_key: str, model: str,
) -> str:
    """发送单个音频段落 + 帧到多模态模型，返回段落总结。"""
    import base64 as b64

    mp3_b64 = b64.b64encode(chunk_mp3.read_bytes()).decode()
    meta_block = ""
    if metadata:
        meta_block = f"【标题】{metadata.get('title', '')}\n【简介】{metadata.get('desc', '')}\n"

    # 筛选本段落时间范围内的帧
    chunk_frames = [
        f for f in frames
        if _frame_timestamp(f) is None or time_start <= _frame_timestamp(f) <= time_end
    ]
    # 如果没有帧时间戳或没有匹配帧，取前几帧（跨段落共享视觉信息）
    if not chunk_frames and frames:
        chunk_frames = frames[:4]

    prompt = (
        f"{meta_block}"
        f"【视频段落 {chunk_index}/{total_chunks}】时间范围 {time_start:.0f}s–{time_end:.0f}s\n"
        f"请聆听该段落的音频并结合画面帧，输出 2-5 句话的段落总结。"
    )

    content_parts: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}},
    ]
    for f in chunk_frames[:6]:  # 每段帧数上限
        img_b64 = b64.b64encode(f.read_bytes()).decode()
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "low"},
        })

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CHUNK_SUMMARY_SYS},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }
    # 流式输出段落摘要（用于实时进度）
    return _chat_completion_stream(base_url, api_key, payload, timeout=300)


def _frame_timestamp(frame_path: Path) -> float | None:
    """从帧文件名提取时间戳（如 frame_01_30s.jpg → 30.0）。"""
    import re
    m = re.search(r"(\d+)s", frame_path.stem)
    return float(m.group(1)) if m else None


def _merge_summaries(
    chunk_summaries: list[str], metadata: dict,
    base_url: str, api_key: str, model: str,
) -> str:
    """将多个段落摘要合并为完整总结。"""
    if len(chunk_summaries) <= 1:
        return chunk_summaries[0] if chunk_summaries else ""

    meta_block = ""
    if metadata:
        meta_block = f"【标题】{metadata.get('title', '')}\n【简介】{metadata.get('desc', '')}\n"

    parts = "\n\n---\n\n".join(
        f"**段落 {i+1}**：{s}" for i, s in enumerate(chunk_summaries)
    )
    prompt = f"{meta_block}\n请合并以下段落摘要为完整总结：\n\n{parts}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _MERGE_SYS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    # 更新 live summary 显示合并阶段
    _live_summary.set(_live_summary.partial + "\n\n--- 合并中… ---\n\n")
    merged = _chat_completion_stream(base_url, api_key, payload, timeout=180)
    return merged


def _summarize_multimodal(
    mp3_path: Path, metadata: dict, video_path: Path | None = None,
) -> str:
    """音频直送多模态模型（+ 自适应关键帧），跳过 ASR 转写。

    将 mp3 作为 input_audio，搭配按时长自适应采样的关键帧，
    一起发给多模态 LLM（如 Qwen3-Omni），模型原生理解音视频内容并总结。

    超长音频（>_MAX_AUDIO_CHUNK_SECONDS）自动分块处理：
    切分为多个段落 → 逐段总结 → 合并为完整总结。
    """
    base_url = settings.multimodal_base_url or settings.openai_base_url
    api_key = settings.openai_api_key
    model = settings.multimodal_model or settings.llm_model
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")

    # ── 关键帧抽取（提前做，供所有段落共享）──
    all_frames: list[Path] = []
    if video_path and video_path.exists():
        try:
            from vidagent.utils.frames import extract_frames
            all_frames = extract_frames(video_path)
            logger.info("多模态总结：%d 帧画面已抽取", len(all_frames))
        except Exception as e:
            logger.warning("关键帧抽取失败，降级为纯音频: %s", e)

    # ── 检查音频时长，决定是否分块 ──
    duration = _get_audio_duration(mp3_path)
    if duration > _MAX_AUDIO_CHUNK_SECONDS:
        logger.info("长音频检测：%.0fs → 按 %ds 分块处理", duration, _MAX_AUDIO_CHUNK_SECONDS)
        return _summarize_multimodal_chunked(
            mp3_path=mp3_path, duration=duration,
            metadata=metadata, all_frames=all_frames,
            base_url=base_url, api_key=api_key, model=model,
        )

    # ── 短音频：单次请求（流式输出到 _live_summary）──
    mp3_b64 = base64.b64encode(mp3_path.read_bytes()).decode()
    logger.info(
        "多模态总结：音频 %s (%d KB) → base64 %d KB",
        mp3_path.name, mp3_path.stat().st_size // 1024, len(mp3_b64) // 1024,
    )

    meta_block = ""
    if metadata:
        meta_block = f"【标题】{metadata.get('title', '')}\n【简介】{metadata.get('desc', '')}\n"

    prompt_text = f"{meta_block}\n请结合音频和关键帧画面，输出结构化总结。"

    content_parts: list[dict] = [
        {"type": "text", "text": prompt_text},
        {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}},
    ]
    for f in all_frames:
        img_b64 = base64.b64encode(f.read_bytes()).decode()
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}",
                "detail": "low",
            },
        })

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYS_MULTIMODAL},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    # 流式：逐 token 更新 _live_summary，UI 可实时轮询
    return _chat_completion_stream(base_url, api_key, payload, timeout=300)


def _summarize_multimodal_chunked(
    mp3_path: Path, duration: float, metadata: dict,
    all_frames: list[Path], base_url: str, api_key: str, model: str,
) -> str:
    """长音频分块处理：切分 → 逐段总结 → 合并。"""
    import tempfile
    from pathlib import Path as P

    work_dir = P(tempfile.mkdtemp(prefix="vidagent_chunks_"))
    try:
        # 自适应分块大小：≤10 分钟，至少 2 段
        chunk_s = min(_MAX_AUDIO_CHUNK_SECONDS, max(300, duration / 6))
        audio_chunks = _split_audio(mp3_path, int(chunk_s), work_dir)
        total = len(audio_chunks)
        logger.info("长音频分块完成：%d 段（每段 ~%ds）", total, chunk_s)

        chunk_summaries: list[str] = []
        for i, chunk_path in enumerate(audio_chunks, 1):
            t_start = (i - 1) * chunk_s
            t_end = min(i * chunk_s, duration)
            logger.info("多模态总结：段落 %d/%d (%.0fs–%.0fs) …", i, total, t_start, t_end)

            # 更新实时进度（覆盖旧段落内容）
            progress_header = (
                f"🎵 长视频分块总结中… 段落 {i}/{total} ({(i-1)/total*100:.0f}%)\n"
                f"{'─' * 40}\n\n"
            )
            if chunk_summaries:
                # 已完成的段落保持可见
                done_text = "\n\n".join(
                    f"**段落 {j+1}** ({j*chunk_s:.0f}s–{min((j+1)*chunk_s, duration):.0f}s)：\n{s[:200]}…"
                    for j, s in enumerate(chunk_summaries)
                )
                _live_summary.set(progress_header + done_text + f"\n\n⏳ 段落 {i} 分析中…")
            else:
                _live_summary.set(progress_header + f"⏳ 段落 {i} 分析中…")

            summary = _summarize_chunk(
                chunk_mp3=chunk_path,
                chunk_index=i, total_chunks=total,
                time_start=t_start, time_end=t_end,
                metadata=metadata, frames=all_frames,
                base_url=base_url, api_key=api_key, model=model,
            )
            chunk_summaries.append(summary)

        # 合并段落摘要
        logger.info("长音频分块总结完成，合并 %d 段摘要…", total)
        return _merge_summaries(
            chunk_summaries, metadata,
            base_url=base_url, api_key=api_key, model=model,
        )
    finally:
        # 清理临时文件
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
