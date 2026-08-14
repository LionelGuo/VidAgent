"""长视频多模态管线（#4 深模块：自原 summarizer.py 拆出）。

音频直送多模态模型（+ 自适应关键帧），超长音频按 base64 大小阈值分块：
切分 → 逐段总结（流式写入 progress.chunks）→ 合并。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from vidagent import llm_provider
from vidagent.tools.summarize.progress import Progress, ProgressStage
from vidagent.tools.summarize.prompts import _CHUNK_SUMMARY_SYS, _MERGE_SYS, _SUMMARY_SYS_MULTIMODAL
from vidagent.tools.summarize.transport import _chat_completion_stream
from vidagent.utils.frames import extract_frames

logger = logging.getLogger(__name__)

# 长音频分块阈值：base64 超过此大小按段落分片处理（每段独立请求 + 最终合并）
# vLLM-Omni multimodal cache 有大小限制，单段过大会触发 AssertionError
# 16kHz mono -q:a 7 下：1h ≈ 15MB mp3 ≈ 20MB base64，单请求可处理
_MAX_AUDIO_B64_KB = 20 * 1024  # 20 MB base64 ≈ 15 MB mp3（约 1h 16kHz mono）


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
    progress: Progress | None = None,
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
        f"请聆听该段落的音频并结合画面帧，输出 150-300 字的详细段落总结，覆盖关键信息与细节。"
    )

    content_parts: list[dict] = [
        {"type": "text", "text": prompt},
        llm_provider.build_audio_part(mp3_b64),
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
    progress: Progress | None = None,
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
    pg = progress
    pg.set(pg.partial + "\n\n--- 合并中… ---\n\n")
    merged = _chat_completion_stream(base_url, api_key, payload, timeout=180, progress=progress)
    return merged


def _summarize_multimodal(
    mp3_path: Path,
    metadata: dict,
    video_path: Path | None = None,
    pre_extracted_frames: list[Path] | None = None,
    progress: Progress | None = None,
) -> str:
    """音频直送多模态模型（+ 自适应关键帧），跳过 ASR 转写。

    将 mp3 作为 input_audio，搭配按时长自适应采样的关键帧，
    一起发给多模态 LLM（如 Qwen3-Omni），模型原生理解音视频内容并总结。

    超长音频（>_MAX_AUDIO_CHUNK_SECONDS）自动分块处理：
    切分为多个段落 → 逐段总结 → 合并为完整总结。
    """
    _ep = llm_provider.agent_endpoint()
    base_url, api_key, model = _ep.base_url, _ep.api_key, _ep.model
    if not api_key:
        raise RuntimeError("未配置 LLM_API_KEY")

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

    content_parts.insert(0, llm_provider.build_audio_part(mp3_b64))
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
    # 流式：逐 token 更新 progress，SSE 可实时轮询
    return _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)


def _summarize_multimodal_chunked(
    mp3_path: Path, duration: float, metadata: dict,
    all_frames: list[Path], base_url: str, api_key: str, model: str,
    progress: Progress | None = None,
) -> str:
    """长音频分块处理：切分 → 逐段总结 → 合并。"""
    import tempfile
    from pathlib import Path as P

    work_dir = P(tempfile.mkdtemp(prefix="vidagent_chunks_"))
    pg = progress
    try:
        # 基于文件大小的自适应分块：每段目标 ~8MB mp3（~10.6MB base64）
        mp3_size = mp3_path.stat().st_size
        target_per_chunk = 8 * 1024 * 1024  # 8 MB mp3 per chunk
        chunk_s = max(300, int(duration * target_per_chunk / max(mp3_size, 1)))
        audio_chunks = _split_audio(mp3_path, int(chunk_s), work_dir)
        total = len(audio_chunks)
        logger.info("长音频分块完成：%d 段（每段 ~%ds）", total, chunk_s)

        # 初始化分块进度（前端渲染分段圆角框）
        pg.chunks = []
        pg.current_chunk = -1
        pg.stage = ProgressStage.CHUNKING
        pg.set("")

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

            # 注册分块进度条目，流式内容写入该条目
            pg.chunks.append({
                "index": i,
                "total": total,
                "time_start": int(t_start),
                "time_end": int(t_end),
                "status": "waiting",
                "text": "",
            })
            pg.current_chunk = i - 1

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
            pg.chunks[-1]["status"] = "done"
            pg.chunks[-1]["text"] = summary
            logger.info(
                "✅ 分块 %d/%d 完成: %.1fs (%d 字)",
                i, total, chunk_elapsed, len(summary),
            )

        # 分块全部完成，进入合并阶段
        pg.current_chunk = -1
        pg.stage = ProgressStage.MERGING
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
