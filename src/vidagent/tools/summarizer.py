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
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from vidagent.config import settings
from vidagent.utils import storage
from vidagent.utils.frames import extract_frames
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
        self.stage = ""         # 当前阶段: downloading | extracting | summarizing
        self.download_pct = 0   # 下载进度 0-100

    def begin(self, label: str = "🎵 多模态总结中…") -> None:
        self.active = True
        self.partial = label + "\n\n"
        self.stage = "summarizing"

    def append(self, text: str) -> None:
        self.partial += text

    def set(self, text: str) -> None:
        """替换全部（用于 chunk 进度更新）。"""
        self.partial = text

    def reset(self) -> None:
        self.active = False
        self.partial = ""
        self.stage = ""


_live = _LiveASR()
_live_summary = _LiveSummary()

# per-task progress（替代全局单例，支持并行总结）
_task_progress: dict[str, _LiveSummary] = {}


def create_progress(task_id: str) -> _LiveSummary:
    """创建一个 per-task 进度追踪器，存入全局 dict。"""
    tp = _LiveSummary()
    _task_progress[task_id] = tp
    return tp


def get_progress(task_id: str) -> _LiveSummary | None:
    """获取 per-task 进度追踪器。"""
    return _task_progress.get(task_id)


def cleanup_progress(task_id: str) -> None:
    """清理 per-task 进度追踪器。"""
    _task_progress.pop(task_id, None)


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
    "请直接聆听音频、观察画面，然后用中文输出详细的、结构化的总结。\n"
    "1. **核心观点**（1-3 条，最关键的结论或主张）\n"
    "2. **主要内容梳理**（按逻辑分点，详细展开，尽量覆盖视频中所有重要信息，不要遗漏细节）\n"
    "3. **关键帧画面描述**（简要描述各帧的视觉内容）\n"
    "请优先基于音频内容进行总结（音频通常包含主要信息），"
    "关键帧作为视觉补充。请输出完整、详细的总结，至少800字。"
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

_SUMMARY_SYS_CHAPTER = (
    "你是一个专业的视频内容分析师。你会收到视频的完整音频和关键帧画面。\n"
    "请聆听音频、观察画面，然后将视频划分为 3-8 个话题段落。\n\n"
    "输出格式（每个段落以 ## 开头）：\n"
    "## 开场介绍\n"
    "主持人介绍本期主题和嘉宾背景，现场气氛轻松...\n\n"
    "## 核心讨论\n"
    "三位嘉宾围绕AI伦理展开激烈辩论，主要观点包括...\n\n"
    "## 总结展望\n"
    "主持人对讨论要点进行总结并展望未来趋势...\n\n"
    "关键规则：\n"
    "- **绝对不要输出任何时间戳、秒数或 MM:SS 格式的时间**\n"
    "- 画面标注 [画面 @ Xs] 仅供你理解时间顺序，不要在输出中引用这些数字\n"
    "- 段落按时间先后顺序排列\n"
    "- 描述要包含实际内容和关键观点，而非泛泛而谈"
)

_SUMMARY_SYS_SHORT = (
    "你是一个专业的短视频内容分析师。你会收到短视频的完整音频和画面。\n"
    "短视频信息密度极高，每一帧都可能包含关键信息。\n"
    "请仔细聆听音频、观察所有画面细节，输出精准详细的总结：\n\n"
    "1. **视频主题**（一句话概括核心内容）\n"
    "2. **关键信息点**（逐条列出视频中的重要信息、数据、观点、步骤，不遗漏细节）\n"
    "3. **画面分析**（关键视觉元素：场景变化、屏幕文字、人物动作、转场等）\n\n"
    "请输出完整详尽的分析，覆盖视频中所有有价值的信息。短视频不容错过任何细节。"
)

# 长音频分块阈值：base64 超过此大小按段落分片处理（每段独立请求 + 最终合并）
# vLLM-Omni multimodal cache 有大小限制，单段过大会触发 AssertionError
# 16kHz mono -q:a 7 下：1h ≈ 15MB mp3 ≈ 20MB base64，单请求可处理
_MAX_AUDIO_B64_KB = 20 * 1024  # 20 MB base64 ≈ 15 MB mp3（约 1h 16kHz mono）


def _chat_completion(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
) -> str:
    """非流式 chat completion，返回完整响应文本（自动剥离 <think> 块）。"""
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 调用失败 HTTP {resp.status_code}: {resp.text[:300]}")
    raw = resp.json()["choices"][0]["message"]["content"]
    usage = resp.json().get("usage", {})
    stripped = _strip_think_blocks(raw)
    elapsed = time.perf_counter() - t0
    think_len = len(raw) - len(stripped)
    logger.info(
        "📡 非流式响应: %.1fs, raw=%d stripped=%d (think=%d, %d%%) | completion_tokens=%s",
        elapsed, len(raw), len(stripped), think_len,
        int(think_len / max(len(raw), 1) * 100),
        usage.get("completion_tokens", "?"),
    )
    return stripped


def _strip_think_blocks(text: str) -> str:
    """从完整文本中剥离所有 <think>…</think> 块。"""
    import re as _re
    # 处理可能跨行的 think 块
    result = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
    return result.strip()


class _ThinkStripper:
    """流式 <think> 块过滤器：逐 token 输入，自动分离推理和实际内容。

    Thinking 模型输出格式：<think>推理过程...</think>实际内容
    通过 on_thinking 回调流式透传推理过程，降低感知延迟。
    """

    def __init__(self, on_thinking=None) -> None:
        self._buffer = ""
        self._in_think = False
        self._think_tag_end = 0  # <think> 标签结束位置，用于后续提取推理内容
        self._on_thinking = on_thinking  # 回调: (text_chunk)

    def feed(self, token: str) -> str | None:
        """输入一个 token，返回剥离后的纯文本（可能为 None）。

        同时通过 on_thinking 回调逐段推送推理内容。
        """
        self._buffer += token
        output = ""

        if not self._in_think:
            # 检查是否进入 <think> 块
            tp = self._buffer.find("<think")
            if tp != -1:
                output = self._buffer[:tp]
                self._buffer = self._buffer[tp:]
                self._in_think = True
                # 记录 <think> 标签的结束位置
                tag_close = self._buffer.find(">")
                self._think_tag_end = tag_close + 1 if tag_close != -1 else len("<think>")
            else:
                # 防止 <think 被截断：保留最后 6 个字符
                safe = max(0, len(self._buffer) - 6)
                for i in range(6, 0, -1):
                    if "<think"[:i] == self._buffer[-i:]:
                        safe = max(0, len(self._buffer) - i)
                        break
                output = self._buffer[:safe]
                self._buffer = self._buffer[safe:]

        if self._in_think:
            # 检查是否退出 <think> 块
            ep = self._buffer.find("</think>")
            if ep != -1:
                # 提取推理内容（<think> 标签后、</think> 之前）
                reasoning = self._buffer[self._think_tag_end:ep]
                if reasoning and self._on_thinking:
                    self._on_thinking(reasoning)
                after = self._buffer[ep + len("</think>"):]
                self._buffer = after
                self._in_think = False
                self._think_tag_end = 0
                # 递归处理剩余内容
                if after:
                    rest = self.feed("")
                    if rest:
                        output = (output + rest) if output else rest
                return output if output else None
            else:
                # 仍在 think 内：流式推送推理内容（批量，降低回调频率）
                if self._think_tag_end > 0 and len(self._buffer) > self._think_tag_end:
                    safe_end = max(self._think_tag_end, len(self._buffer) - 7)
                    pending = safe_end - self._think_tag_end
                    # 批量推送：≥16 字符 或 缓冲区末尾有换行
                    if pending >= 16 or (pending > 0 and "\n" in self._buffer[self._think_tag_end:safe_end]):
                        chunk = self._buffer[self._think_tag_end:safe_end]
                        if chunk and self._on_thinking:
                            self._on_thinking(chunk)
                        self._buffer = self._buffer[:self._think_tag_end] + self._buffer[safe_end:]

        return output if output else None

    def flush(self) -> str | None:
        """流结束时清空缓冲区。"""
        if self._in_think:
            # 未闭合的 <think>：作为推理内容推送
            reasoning = self._buffer[self._think_tag_end:] if self._think_tag_end else self._buffer
            if reasoning and self._on_thinking:
                self._on_thinking(reasoning)
            logger.debug("_ThinkStripper: 未闭合 <think>，已推送 %d chars", len(reasoning))
            self._buffer = ""
            self._in_think = False
            self._think_tag_end = 0
            return None
        if self._buffer:
            out = self._buffer
            self._buffer = ""
            return out
        return None


def _chat_completion_stream(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
    progress: _LiveSummary | None = None,
) -> str:
    """流式 chat completion，返回完整响应文本；同时更新 progress（默认 _live_summary）。"""
    payload = {**payload, "stream": True}
    accumulated = ""
    accumulated_raw = ""  # 诊断：未过滤的原始内容
    token_count = 0
    t0 = time.perf_counter()
    ttft = None  # time-to-first-token (first non-think content token)
    pg = progress or _live_summary  # 默认回退到全局单例，保持向后兼容

    # 思考过程回调：更新进度显示，降低感知延迟
    thinking_shown = False

    def _on_thinking(text: str) -> None:
        nonlocal thinking_shown
        if not thinking_shown:
            pg.set("🤔 思考中...\n\n")
            thinking_shown = True
        # 逐步追加推理内容（限制长度防止 UI 过载）
        current = pg.partial
        if len(current) < 2000:
            pg.append(text)

    stripper = _ThinkStripper(on_thinking=_on_thinking)

    with httpx.stream(
        "POST", f"{base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 流式调用失败 HTTP {resp.status_code}: {resp.text[:300]}")
        finish_reason = None
        for line in resp.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    # 追踪 finish_reason（诊断截断根因）
                    fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                    token = delta.get("content", "")
                    if token:
                        accumulated_raw += token  # 诊断：保留原始内容
                        # 分离 <think> 推理块和实际内容
                        stripped = stripper.feed(token)
                        if stripped:
                            if ttft is None:
                                ttft = time.perf_counter() - t0
                                # 第一个实际内容 token 到达：清空思考显示
                                if thinking_shown:
                                    pg.set("")
                                    thinking_shown = False
                            accumulated += stripped
                            token_count += len(stripped)
                            pg.append(stripped)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

    # 流结束：清空 stripper 残留，同时推送到 progress
    flushed = stripper.flush()
    flushed_len = len(flushed) if flushed else 0
    if flushed:
        if ttft is None:
            ttft = time.perf_counter() - t0
        accumulated += flushed
        token_count += len(flushed)
        pg.append(flushed)  # 修复：最后几个字符也要推送到前端

    elapsed = time.perf_counter() - t0
    raw_len = len(accumulated_raw)
    stripped_len = len(accumulated)
    logger.info(
        "📡 vLLM 响应: %d tokens / %.1fs (%.0f tok/s), TTFT %.2fs, raw=%d stripped=%d (flush=%d) chars | finish_reason=%s",
        token_count, elapsed, token_count / max(elapsed, 0.01),
        ttft or 0, raw_len, stripped_len, flushed_len, finish_reason or "?",
    )
    if finish_reason == "length":
        logger.warning("⚠️ vLLM 返回 finish_reason=length，输出被截断！可能需要增大 --max-num-batched-tokens")
    think_pct = int((1 - stripped_len / max(raw_len, 1)) * 100)
    logger.info(
        "  思考占 %d%%（%d chars）| 实际总结 %d chars",
        think_pct, raw_len - stripped_len, stripped_len,
    )
    return accumulated


def extract_and_summarize(
    local_path: str,
    metadata: dict | None = None,
    task_id: str | None = None,
    candidate_boundaries: list[int] | None = None,
    candidate_frames: list[str] | None = None,
) -> dict:
    """对本地视频生成结构化中文总结（Markdown）。

    多模态模型（LLM_MULTIMODAL=true）：抽取音频 → 直送 LLM，跳过 ASR。
    普通模型：抽取音频 → ASR 转写 → 文本总结。

    无音频轨时自动降级为仅依据元数据的总结（不报错）。

    Args:
        local_path: 本地视频文件路径（用 download_video 返回的 local_path）。
        metadata: 视频元数据，至少含 title 与 desc。
        task_id: 可选，per-task 进度追踪 ID。传入时创建独立 progress 实例。
        candidate_boundaries: 可选，候选章节边界列表（秒）。传入时启用章节感知总结。
        candidate_frames: 可选，候选边界处的帧路径列表（配合 candidate_boundaries 使用）。

    Returns:
        {"summary": str, "chapters": [{"start": int, "end": int, "title": str}]}
    """
    metadata = metadata or {}
    video_id = metadata.get("video_id", "")

    # per-task progress（替代全局单例，支持并行 + 前端流式）
    progress = create_progress(task_id) if task_id else None
    try:
        if progress:
            progress.begin()

        # ── 多模态路径：音频直送 LLM，跳过 ASR ──
        if settings.llm_multimodal:
            _live.begin()
            _live.update("🎵 多模态模型分析音频中…")
            if not progress:
                _live_summary.begin()
            try:
                video_path = Path(local_path)

                # ── 章节感知路径：使用预提取的候选帧和边界 ──
                if candidate_boundaries and candidate_frames:
                    # 音频提取（只做音频，帧已预提取）
                    with Timer("音频提取(ffmpeg)"):
                        mp3 = extract_audio(local_path)

                    mp3_kb = Path(mp3).stat().st_size // 1024
                    frames_paths = [Path(f) for f in candidate_frames]
                    frames_kb = sum(f.stat().st_size for f in frames_paths) // 1024
                    logger.info(
                        "⚙️ 预处理完成(章节模式): 音频 %d KB + %d 候选帧 / %d KB",
                        mp3_kb, len(frames_paths), frames_kb,
                    )

                    base_url = settings.multimodal_base_url or settings.openai_base_url
                    api_key = settings.openai_api_key
                    model = settings.multimodal_model or settings.llm_model

                    with Timer("多模态总结(章节感知)"):
                        chapters, summary = _summarize_multimodal_with_chapters(
                            mp3_path=Path(mp3),
                            metadata=metadata,
                            candidate_boundaries=candidate_boundaries,
                            candidate_frames=frames_paths,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            progress=progress,
                        )
                    return {"summary": summary, "chapters": chapters}

                # ── 原多模态路径（无章节）──
                # 并行：音频提取 + 帧抽取（两个独立 ffmpeg 操作）
                from concurrent.futures import ThreadPoolExecutor

                t0_pre = time.perf_counter()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    audio_future = pool.submit(extract_audio, local_path)
                    frames_future = pool.submit(
                        extract_frames, video_path,
                        duration=metadata.get("duration"),
                    )
                    mp3 = audio_future.result()
                    all_frames = frames_future.result()
                pre_elapsed = time.perf_counter() - t0_pre

                mp3_kb = Path(mp3).stat().st_size // 1024
                frames_kb = sum(f.stat().st_size for f in all_frames) // 1024
                logger.info(
                    "⚙️ 预处理完成: 音频 %d KB + %d 帧 / %d KB | %.1fs (并行)",
                    mp3_kb, len(all_frames), frames_kb, pre_elapsed,
                )

                with Timer("多模态总结(音频直送)"):
                    summary = _summarize_multimodal(
                        Path(mp3), metadata,
                        video_path=video_path,
                        pre_extracted_frames=all_frames,
                        progress=progress,
                    )
                    return {"summary": summary, "chapters": []}
            except Exception as e:
                logger.warning("多模态总结失败，走降级总结（仅元数据）: %s", e)
                with Timer("LLM 总结(降级)"):
                    return {"summary": _summarize("", metadata), "chapters": []}
            finally:
                _live.reset()
                if not progress:
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
            return {"summary": _summarize(transcript, metadata), "chapters": []}
    finally:
        if progress:
            progress.reset()


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
    raw = resp.json()["choices"][0]["message"]["content"]
    return _strip_think_blocks(raw)


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
             "-i", str(mp3_path), "-c:a", "libmp3lame", "-q:a", "7",
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
    progress: _LiveSummary | None = None,
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
        # max_tokens 由 vLLM --max-num-batched-tokens 统一限制
    }
    # 流式输出段落摘要（用于实时进度）
    return _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)


def _frame_timestamp(frame_path: Path) -> float | None:
    """从帧文件名提取时间戳（如 frame_01_30s.jpg → 30.0）。"""
    import re
    m = re.search(r"(\d+)s", frame_path.stem)
    return float(m.group(1)) if m else None


def _merge_summaries(
    chunk_summaries: list[str], metadata: dict,
    base_url: str, api_key: str, model: str,
    progress: _LiveSummary | None = None,
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
        # max_tokens 由 vLLM --max-num-batched-tokens 统一限制
    }
    # 更新 summary 显示合并阶段
    pg = progress or _live_summary
    pg.set(pg.partial + "\n\n--- 合并中… ---\n\n")
    merged = _chat_completion_stream(base_url, api_key, payload, timeout=180, progress=progress)
    return merged


def _summarize_multimodal(
    mp3_path: Path,
    metadata: dict,
    video_path: Path | None = None,
    pre_extracted_frames: list[Path] | None = None,
    progress: _LiveSummary | None = None,
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

    # ── 关键帧（使用预提取的，或现场抽取）──
    all_frames: list[Path] = pre_extracted_frames or []
    if not all_frames and video_path and video_path.exists():
        try:
            all_frames = extract_frames(video_path)
            logger.info("多模态总结：%d 帧画面已抽取", len(all_frames))
        except Exception as e:
            logger.warning("关键帧抽取失败，降级为纯音频: %s", e)

    # ── 时长 + 分块决策：基于 base64 大小而非时长 ──
    duration = _get_audio_duration(mp3_path)
    mp3_size = mp3_path.stat().st_size
    b64_estimate = int(mp3_size * 4 / 3) // 1024  # base64 ≈ 133% of binary

    if b64_estimate > _MAX_AUDIO_B64_KB:
        logger.info(
            "长音频检测：%.0fs / %d KB mp3 → ~%d KB base64 → 分块处理",
            duration, mp3_size // 1024, b64_estimate,
        )
        return _summarize_multimodal_chunked(
            mp3_path=mp3_path, duration=duration,
            metadata=metadata, all_frames=all_frames,
            base_url=base_url, api_key=api_key, model=model,
            progress=progress,
        )

    # ── 短音频：单次请求（流式输出到 progress）──
    t0_encode = time.perf_counter()
    mp3_b64 = base64.b64encode(mp3_path.read_bytes()).decode()
    audio_b64_kb = len(mp3_b64) // 1024
    encode_elapsed = time.perf_counter() - t0_encode

    # 帧 base64
    frames_b64_kb = 0
    t0_frames_encode = time.perf_counter()
    content_parts: list[dict] = []
    for f in all_frames:
        img_b64 = base64.b64encode(f.read_bytes()).decode()
        frames_b64_kb += len(img_b64) // 1024
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}",
                "detail": "low",
            },
        })
    frames_encode_elapsed = time.perf_counter() - t0_frames_encode

    meta_block = ""
    if metadata:
        meta_block = f"【标题】{metadata.get('title', '')}\n【简介】{metadata.get('desc', '')}\n"
    prompt_text = f"{meta_block}\n请结合音频和关键帧画面，输出结构化总结。"

    content_parts.insert(0, {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}})
    content_parts.insert(0, {"type": "text", "text": prompt_text})

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYS_MULTIMODAL},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
    }
    payload_kb = len(json.dumps(payload, ensure_ascii=False)) // 1024

    logger.info(
        "📦 发送多模态请求: 音频 %d KB (base64) + %d 帧 / %d KB | "
        "编码 %.2fs (音频) + %.2fs (帧) | 总 payload %d KB → %s",
        audio_b64_kb, len(all_frames), frames_b64_kb,
        encode_elapsed, frames_encode_elapsed, payload_kb, base_url,
    )
    # 流式：逐 token 更新 progress（或全局 _live_summary），UI 可实时轮询
    return _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)


def _summarize_multimodal_chunked(
    mp3_path: Path, duration: float, metadata: dict,
    all_frames: list[Path], base_url: str, api_key: str, model: str,
    progress: _LiveSummary | None = None,
) -> str:
    """长音频分块处理：切分 → 逐段总结 → 合并。"""
    import tempfile
    from pathlib import Path as P

    work_dir = P(tempfile.mkdtemp(prefix="vidagent_chunks_"))
    pg = progress or _live_summary
    try:
        # 基于文件大小的自适应分块：每段目标 ~8MB mp3（~10.6MB base64）
        mp3_size = mp3_path.stat().st_size
        target_per_chunk = 8 * 1024 * 1024  # 8 MB mp3 per chunk
        chunk_s = max(300, int(duration * target_per_chunk / max(mp3_size, 1)))
        audio_chunks = _split_audio(mp3_path, int(chunk_s), work_dir)
        total = len(audio_chunks)
        logger.info("长音频分块完成：%d 段（每段 ~%ds）", total, chunk_s)

        chunk_summaries: list[str] = []
        t0_chunks = time.perf_counter()
        for i, chunk_path in enumerate(audio_chunks, 1):
            t_start = (i - 1) * chunk_s
            t_end = min(i * chunk_s, duration)
            chunk_kb = chunk_path.stat().st_size // 1024
            logger.info(
                "📦 分块 %d/%d: %.0fs–%.0fs (%d KB mp3) → vLLM …",
                i, total, t_start, t_end, chunk_kb,
            )

            # 更新实时进度（覆盖旧段落内容）
            progress_header = (
                f"🎵 长视频分块总结中… 段落 {i}/{total} ({(i-1)/total*100:.0f}%)\n"
                f"{'─' * 40}\n\n"
            )
            if chunk_summaries:
                done_text = "\n\n".join(
                    f"**段落 {j+1}** ({j*chunk_s:.0f}s–{min((j+1)*chunk_s, duration):.0f}s)：\n{s[:200]}…"
                    for j, s in enumerate(chunk_summaries)
                )
                pg.set(progress_header + done_text + f"\n\n⏳ 段落 {i} 分析中…")
            else:
                pg.set(progress_header + f"⏳ 段落 {i} 分析中…")

            t0_chunk = time.perf_counter()
            summary = _summarize_chunk(
                chunk_mp3=chunk_path,
                chunk_index=i, total_chunks=total,
                time_start=t_start, time_end=t_end,
                metadata=metadata, frames=all_frames,
                base_url=base_url, api_key=api_key, model=model,
                progress=progress,
            )
            chunk_elapsed = time.perf_counter() - t0_chunk
            chunk_summaries.append(summary)
            logger.info(
                "✅ 分块 %d/%d 完成: %.1fs (%d 字)",
                i, total, chunk_elapsed, len(summary),
            )

        chunks_total = time.perf_counter() - t0_chunks
        logger.info("分块总结全部完成: %d 段 / %.1fs，开始合并…", total, chunks_total)
        return _merge_summaries(
            chunk_summaries, metadata,
            base_url=base_url, api_key=api_key, model=model,
            progress=progress,
        )
    finally:
        # 清理临时文件
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 章节时间轴：候选锚点 + 模型单次约束选择
# ---------------------------------------------------------------------------


def _parse_chapter_response(
    text: str, candidates: list[int],
) -> tuple[list[dict], str]:
    """从模型输出中解析 CHAPTERS JSON 和 SUMMARY Markdown。

    Args:
        text: 模型输出的原始文本（含 <<<CHAPTERS>>> 和 <<<SUMMARY>>> 标记）。
        candidates: 候选边界时间戳列表（用于校验和修正）。

    Returns:
        (chapters: [{start, end, title}], summary_text: str)
        解析失败时 chapters 为空列表，summary_text 为原始文本。
    """
    import json as _json

    chapters: list[dict] = []
    summary_text = text  # 默认返回原始文本

    # ── 提取 CHAPTERS JSON ──
    chap_m = re.search(r"<<<CHAPTERS>>>\s*(.*?)\s*<<<END_CHAPTERS>>>", text, re.DOTALL)
    if chap_m:
        raw_json = chap_m.group(1).strip()
        # 去掉可能的 markdown 代码块包裹 (```json ... ```)
        code_block_m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_json, re.DOTALL)
        if code_block_m:
            raw_json = code_block_m.group(1).strip()
        try:
            data = _json.loads(raw_json)
            # 兼容三种格式：{"chapters": [...]}, [...], 或 {"start":...} 单对象
            if isinstance(data, list):
                raw_chapters = data
            elif isinstance(data, dict):
                # {"chapters": [...]} 或直接是单个章节对象
                raw_chapters = data.get("chapters", [])
                if not raw_chapters and "start" in data:
                    raw_chapters = [data]  # 单个章节对象
            else:
                raw_chapters = []
        except (_json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning("CHAPTERS JSON 解析失败: %s — 原始: %s", e, raw_json[:200])
            raw_chapters = []

        # 校验 + 修正
        for ch in raw_chapters:
            start = int(ch.get("start", 0))
            end = int(ch.get("end", 0))
            title = str(ch.get("title", "")).strip().strip("*").strip()

            # 过滤非法章节
            if start >= end or (end - start) < 10:
                continue
            if not title:
                continue

            # 修正 start/end 到最近的候选边界（±3s 容差）
            def _snap(t: int) -> int:
                for c in candidates:
                    if abs(c - t) <= 3:
                        return c
                # 没匹配到候选值 → 找最近的
                if candidates:
                    return min(candidates, key=lambda c: abs(c - t))
                return t

            start = _snap(start)
            end = _snap(end)

            # 去重：相邻章节 start 相同 → 跳过
            if chapters and chapters[-1]["start"] == start:
                continue

            chapters.append({"start": start, "end": end, "title": title})

        if chapters:
            logger.info(
                "📑 解析章节: %d 个 → %s",
                len(chapters),
                " → ".join(f"{ch['start']}s {ch['title']}" for ch in chapters),
            )
        else:
            logger.warning("CHAPTERS 校验后无有效章节（原始 %d 条）", len(raw_chapters))

    # ── 提取 SUMMARY Markdown ──
    summary_m = re.search(r"<<<SUMMARY>>>\s*(.*?)\s*<<<END_SUMMARY>>>", text, re.DOTALL)
    if summary_m:
        summary_text = summary_m.group(1).strip()

    # ── 回退：尝试从叙述格式中解析章节（如 "**开场介绍** (0-34s):"）──
    if not chapters:
        narrative_pattern = re.findall(
            r"([^(\n]+?)\s*\((\d+)\s*-\s*(\d+)\s*s\)",
            text,
        )
        if narrative_pattern:
            for title, start_str, end_str in narrative_pattern:
                # 清理 markdown 标记和多余空白
                title = title.strip().strip("*").strip()
                start = int(start_str)
                end = int(end_str)
                if start >= end or (end - start) < 10 or not title:
                    continue
                # snap 到候选边界
                def _snap_fb(t: int) -> int:
                    if not candidates:
                        return t
                    return min(candidates, key=lambda c: abs(c - t))
                chapters.append({
                    "start": _snap_fb(start),
                    "end": _snap_fb(end),
                    "title": title,
                })
            if chapters:
                logger.info("📑 从叙述格式解析到 %d 个章节", len(chapters))

    return chapters, summary_text


def _summarize_multimodal_with_chapters(
    mp3_path: Path,
    metadata: dict,
    candidate_boundaries: list[int],
    candidate_frames: list[Path],
    base_url: str,
    api_key: str,
    model: str,
    progress: _LiveSummary | None = None,
) -> tuple[list[dict], str]:
    """章节感知的多模态总结：完整音频 + 候选边界帧 → 章节划分 + 时间线总结。

    与 _summarize_multimodal() 的关键区别：
    - 帧前面插入时间戳标注文本
    - System prompt 使用 _SUMMARY_SYS_CHAPTER（含候选边界列表约束）
    - 返回 (chapters, summary_text) 而非纯文本

    长音频（>_MAX_AUDIO_B64_KB）仍走分块路径，但模型在 merge 阶段做章节聚合。
    """
    import base64 as b64

    # Phase 1 only 模式：candidate_boundaries 为空 → 只做总结流式输出
    phase1_only = not candidate_boundaries

    # ── 时长 + 分块决策：基于 base64 大小 ──
    duration = _get_audio_duration(mp3_path)
    mp3_size = mp3_path.stat().st_size
    b64_estimate = int(mp3_size * 4 / 3) // 1024

    if b64_estimate > _MAX_AUDIO_B64_KB:
        # 长音频 → 走 chunked 路径，但改为章节感知的 merge prompt
        logger.info(
            "长音频章节总结：%.0fs / %d KB mp3 → ~%d KB base64 → 分块处理",
            duration, mp3_size // 1024, b64_estimate,
        )
        # 走现有的 chunked 流程，但修改 merge prompt 加入章节约束
        summary_text = _summarize_multimodal_chunked(
            mp3_path=mp3_path, duration=duration,
            metadata=metadata, all_frames=candidate_frames,
            base_url=base_url, api_key=api_key, model=model,
            progress=progress,
        )
        # 尝试从合并后的文本中解析章节（如果 merge prompt 也加了标记）
        chapters, summary = _parse_chapter_response(summary_text, candidate_boundaries)
        if not chapters:
            # 未解析到章节 → 用候选边界做均匀切分作为回退
            logger.warning("长音频未解析到章节，回退为均匀切分")
            chapters = _fallback_chapters(candidate_boundaries, int(duration))
        return chapters, summary

    # ── 短音频：单次请求（流式输出）──
    t0_encode = time.perf_counter()
    mp3_b64 = b64.b64encode(mp3_path.read_bytes()).decode()
    audio_b64_kb = len(mp3_b64) // 1024
    encode_elapsed = time.perf_counter() - t0_encode

    # 构建 content_parts：文本提示 + 音频 + 带时间戳标注的帧
    meta_block = ""
    if metadata:
        meta_block = (
            f"【标题】{metadata.get('title', '')}\n"
            f"【简介】{metadata.get('desc', '')}\n"
        )

    prompt_text = (
        f"{meta_block}"
        f"请聆听完整音频，结合关键帧画面，将视频划分为几个话题段落。\n"
        f"每个段落以 ## 标题开头，然后写描述。不要输出任何时间戳。\n"
    )

    content_parts: list[dict] = [
        {"type": "text", "text": prompt_text},
        {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}},
    ]

    # 帧前面插入时间戳标注（帮助模型关联画面和时点）
    frames_b64_kb = 0
    for f in candidate_frames:
        ts = _frame_timestamp(f)
        ts_label = f"[画面 @ {int(ts)}s]" if ts is not None else "[画面]"
        content_parts.append({"type": "text", "text": ts_label})

        img_b64 = b64.b64encode(f.read_bytes()).decode()
        frames_b64_kb += len(img_b64) // 1024
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}",
                "detail": "low",
            },
        })

    # 使用章节专用的 system prompt
    system_prompt = _SUMMARY_SYS_CHAPTER

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
    }
    payload_kb = len(json.dumps(payload, ensure_ascii=False)) // 1024

    logger.info(
        "📦 发送章节总结请求: 音频 %d KB + %d 帧 / %d KB | "
        "候选边界 %d 个 | payload %d KB → %s",
        audio_b64_kb, len(candidate_frames), frames_b64_kb,
        len(candidate_boundaries), payload_kb, base_url,
    )

    # ── 阶段一：多模态模型流式输出总结（用户实时看到）──
    raw_text = _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)

    # Phase 1 only 模式：不做 Phase 2
    if phase1_only:
        chapters, summary = _parse_chapter_response(raw_text, candidate_boundaries)
        return chapters, summary if summary else raw_text

    # 尝试从 Phase 1 输出直接解析（免费，瞬间完成）
    chapters, summary = _parse_chapter_response(raw_text, candidate_boundaries)

    # ── 阶段二：分段多模态匹配 ──
    # 将音频在候选边界处切开，每段配中间帧，让模型做离散段落选择
    if not chapters and len(candidate_boundaries) >= 3:
        logger.info("📑 阶段二：分段多模态匹配 (%d 段) …", len(candidate_boundaries) - 1)
        chapters = _match_chapters_segmented(
            phase1_summary=summary,
            mp3_path=mp3_path,
            candidate_boundaries=candidate_boundaries,
            candidate_frames=candidate_frames,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    # 兜底：均匀切分
    if not chapters and candidate_boundaries:
        logger.warning("章节提取失败，回退为均匀切分")
        chapters = _fallback_chapters(candidate_boundaries, int(duration))

    return chapters, summary


def _split_audio_at_boundaries(
    mp3_path: Path, boundaries: list[int],
) -> list[Path]:
    """在边界点切开音频，返回段文件路径列表（按时间顺序）。"""
    import subprocess as _sp
    import tempfile
    seg_dir = Path(tempfile.mkdtemp(prefix="vidagent_segs_"))
    # ffmpeg segment: -segment_times 接受逗号分隔的秒数（不含 0 和末尾 duration）
    # 使用 boundaries[1:-1] 确保段数 = len(boundaries) - 1
    if len(boundaries) <= 2:
        return []
    times = ",".join(str(b) for b in boundaries[1:-1])
    _sp.run(
        ["ffmpeg", "-y", "-i", str(mp3_path),
         "-f", "segment", "-segment_times", times,
         "-c:a", "libmp3lame", "-q:a", "7",  # 重编码避免帧边界损坏
         str(seg_dir / "seg_%03d.mp3")],
        capture_output=True, timeout=30,
    )
    segs = sorted(seg_dir.glob("seg_*.mp3"))
    logger.info("✂️ 音频切分为 %d 段 → %s", len(segs), seg_dir)
    return segs


def _prepare_short_video(video_path: Path) -> tuple[Path, Path]:
    """预处理短视频：转码 H.264、缩分辨率、降帧率、剥离音频。

    Returns:
        (processed_video_path, audio_path) — 小体积无音轨视频 + 独立音频
    """
    work = Path(tempfile.mkdtemp(prefix="vidagent_short_"))
    processed = work / "video.mp4"

    # 1. 剥离音频并转码视频：384px 宽, 4fps, H.264, 无音轨
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-an",
         "-vf", "scale=384:-2,fps=4",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         str(processed)],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0 or not processed.exists():
        raise RuntimeError(f"短视频转码失败: {r.stderr.decode()[-300:]}")

    # 2. 提取音频（输出到 workspace，复用缓存）
    from vidagent.utils.audio import extract_audio as _extract_audio
    audio_result = _extract_audio(str(video_path))
    if not Path(audio_result).exists():
        raise RuntimeError(f"短视频音频提取失败: {video_path}")

    logger.info(
        "🎬 短视频预处理: 视频 %d KB + 音频 %d KB → %s",
        processed.stat().st_size // 1024,
        Path(audio_result).stat().st_size // 1024,
        work,
    )
    return processed, Path(audio_result)


def _summarize_short_video(
    video_path: Path,
    metadata: dict,
    base_url: str,
    api_key: str,
    model: str,
    progress: _LiveSummary | None = None,
) -> str:
    """短视频总结：预处理后 base64 video_url + 音频 → 单次 LLM 调用。

    与长视频管线的区别：
    - 视觉输入使用 video_url (base64 小视频) 替代 image_url × N
    - 跳过边界检测和 Phase 2 章节匹配
    - 使用短视频专用 prompt（细粒度、不遗漏细节）
    """
    t0 = time.perf_counter()

    # 1. 预处理：转码 + 剥离音频
    processed, mp3_path = _prepare_short_video(video_path)

    # 2. Base64 编码
    t0_encode = time.perf_counter()
    mp3_b64 = base64.b64encode(mp3_path.read_bytes()).decode()
    video_b64 = base64.b64encode(processed.read_bytes()).decode()
    encode_elapsed = time.perf_counter() - t0_encode

    # 3. 构造 content
    meta_block = ""
    if metadata:
        meta_block = (
            f"【标题】{metadata.get('title', '')}\n"
            f"【简介】{metadata.get('desc', '')}\n"
        )

    content_parts: list[dict] = [
        {"type": "text", "text": f"{meta_block}\n请仔细分析这个短视频的音频和画面，输出精准详细的总结。"},
        {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
    ]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUMMARY_SYS_SHORT},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
    }

    video_kb = len(video_b64) // 1024
    audio_kb = len(mp3_b64) // 1024
    payload_kb = len(json.dumps(payload, ensure_ascii=False)) // 1024
    duration = metadata.get("duration", "?")
    logger.info(
        "📦 短视频总结请求: %.0fs | 视频 %d KB + 音频 %d KB (base64) | "
        "编码 %.1fs | payload %d KB → %s",
        duration, video_kb, audio_kb, encode_elapsed, payload_kb, base_url,
    )

    # 4. 流式调用
    summary = _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)

    elapsed = time.perf_counter() - t0
    logger.info("✅ 短视频总结完成: %.0fs | %.1fs 总耗时", duration, elapsed)
    return summary


def _match_chapters_segmented(
    phase1_summary: str,
    mp3_path: Path,
    candidate_boundaries: list[int],
    candidate_frames: list[Path],
    base_url: str,
    api_key: str,
    model: str,
) -> list[dict]:
    """阶段二：分段音频 + 帧 → 直接输出 JSON 章节。

    Thinking 模型的 <think> 推理过程替代了旧的自然语言逐段描述，
    最终答案直接是结构化 JSON。
    """
    import base64 as b64
    import json as _json

    # ── 切分音频 ──
    audio_segs = _split_audio_at_boundaries(mp3_path, candidate_boundaries)
    if len(audio_segs) < 2:
        return []

    M = len(audio_segs)  # 段数

    # ── Phase 2 prompt: 直接输出 JSON ──
    prompt = (
        f"以下视频被切分为 {M} 个片段。背景：{phase1_summary[:800]}\n\n"
        f"请逐段聆听音频、观察画面，将片段归并为几个话题章节。\n"
        f"直接输出 JSON 数组（不要代码块、不要解释）：\n"
        f'[{{"title": "开场介绍", "segments": [1, 2]}}, {{"title": "核心讨论", "segments": [3, 4, 5]}}]\n\n'
        f"规则：\n"
        f"- 段号 1-{M} 必须全部覆盖，相邻章节首尾相接\n"
        f"- title 简洁（10 字以内）\n"
        f"- 只输出 JSON 数组，不要任何其他文字"
    )

    content_parts: list[dict] = [{"type": "text", "text": prompt}]

    for i in range(M):
        content_parts.append({
            "type": "text",
            "text": f"--- 段{i+1} [{candidate_boundaries[i]}s-{candidate_boundaries[i+1]}s] ---",
        })
        seg_b64 = b64.b64encode(audio_segs[i].read_bytes()).decode()
        content_parts.append({
            "type": "input_audio",
            "input_audio": {"data": seg_b64, "format": "mp3"},
        })
        if i < len(candidate_frames):
            img_b64 = b64.b64encode(candidate_frames[i].read_bytes()).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "low"},
            })

    seg_total_kb = sum(s.stat().st_size for s in audio_segs) // 1024
    logger.info("📦 Phase 2: %d 段音频 (%d KB) + %d 帧 → %s", M, seg_total_kb, len(candidate_frames), base_url)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出 JSON 数组。不输出解释、标记或代码块。"},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.2,
        # max_tokens 由 vLLM --max-num-batched-tokens 统一限制
    }
    phase2_raw = _chat_completion(base_url, api_key, payload, timeout=120)
    phase2_raw = phase2_raw.strip()
    logger.info("📑 Phase 2 输出:\n%s", phase2_raw[:600])

    # ── 清理临时文件 ──
    import shutil
    shutil.rmtree(audio_segs[0].parent, ignore_errors=True)

    # ── 解析 JSON ──
    try:
        # 去掉可能的 markdown 代码块
        raw = phase2_raw
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = data.get("chapters", [])
        if isinstance(data, list) and len(data) > 0:
            chapters: list[dict] = []
            for ch in data:
                segs = ch.get("segments", [])
                if not segs:
                    continue
                first = min(segs) - 1
                last = max(segs) - 1
                if first < 0 or last >= len(candidate_boundaries) - 1:
                    continue
                title = str(ch.get("title", "")).strip()
                chapters.append({
                    "start": candidate_boundaries[first],
                    "end": candidate_boundaries[last + 1],
                    "title": title,
                })
            if chapters:
                logger.info("📑 Phase 2 解析成功: %d 个章节", len(chapters))
                return chapters
    except Exception as e:
        logger.warning("Phase 2 JSON 解析失败: %s", e)

    # ── 回退 ──
    return _fallback_chapters(candidate_boundaries, int(candidate_boundaries[-1]))


def _fallback_chapters(candidates: list[int], duration: int) -> list[dict]:
    """回退：当模型未输出章节时，用候选边界做简单的均匀切分。

    选取候选边界中均匀分布的 3-8 个点作为章节起点。
    """
    if len(candidates) <= 2:
        return []

    target = max(3, min(8, len(candidates) // 2))
    step = max(1, len(candidates) // target)
    selected = candidates[::step]

    chapters: list[dict] = []
    for i, start in enumerate(selected):
        end = selected[i + 1] if i + 1 < len(selected) else duration
        chapters.append({
            "start": start,
            "end": end,
            "title": f"段落 {i + 1}",
        })
    return chapters
