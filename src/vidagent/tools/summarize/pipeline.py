"""总结管线公开入口与降级路径（#4 深模块：自原 summarizer.py 拆出）。

extract_and_summarize 是总结域唯一公开入口：抽取音频/帧 → 多模态总结；
失败降级为仅元数据总结（不崩溃）。90s 短视频分流归 #4 Q3（C4 实施）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from vidagent import llm_provider
from vidagent.tools.summarize.chapters import _summarize_multimodal_with_chapters
from vidagent.tools.summarize.multimodal import _summarize_multimodal
from vidagent.tools.summarize.progress import create_progress
from vidagent.tools.summarize.prompts import _SUMMARY_SYS
from vidagent.tools.summarize.transport import _chat_completion
from vidagent.utils.audio import extract_audio
from vidagent.utils.frames import extract_frames
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)


def extract_and_summarize(
    local_path: str,
    metadata: dict | None = None,
    task_id: str | None = None,
    candidate_boundaries: list[int] | None = None,
    candidate_frames: list[str] | None = None,
) -> dict:
    """对本地视频生成结构化中文总结（Markdown）。

    抽取音频 + 关键帧 → 直送多模态 LLM（唯一路径，ASR 已随旧栈删除）。

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

    # per-task progress（支持并行 + 前端流式）
    progress = create_progress(task_id) if task_id else None
    try:
        if progress:
            progress.begin()

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

            _ep = llm_provider.agent_endpoint()
            base_url, api_key, model = _ep.base_url, _ep.api_key, _ep.model

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

        # ── 常规多模态路径（无章节）──
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
        if progress:
            progress.reset()


def _summarize(transcript: str, metadata: dict) -> str:
    _ep = llm_provider.agent_endpoint()
    base_url, api_key, model = _ep.base_url, _ep.api_key, _ep.model
    if not api_key:
        raise RuntimeError(
            f"未配置 LLM API key：请在 .env 设置 LLM_API_KEY。"
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
    # 复用 transport 层非流式调用（原为重复手写的 httpx.post，#4 去重）
    return _chat_completion(base_url, api_key, payload, timeout=120)
