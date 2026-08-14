"""总结管线公开入口与降级路径（#4 深模块：自原 summarizer.py 拆出）。

extract_and_summarize 是总结域唯一公开入口：抽取音频/帧 → 多模态总结；
失败降级为仅元数据总结（不崩溃）。90s 短视频分流归 #4 Q3（C4 实施）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from vidagent import llm_provider
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
) -> str:
    """对本地视频生成结构化中文总结（Markdown）。

    抽取音频 + 关键帧 → 直送多模态 LLM（唯一路径，ASR 已随旧栈删除）。

    无音频轨时自动降级为仅依据元数据的总结（不报错）。

    Args:
        local_path: 本地视频文件路径（用 download_video 返回的 local_path）。
        metadata: 视频元数据，至少含 title 与 desc。
        task_id: 可选，per-task 进度追踪 ID。传入时创建独立 progress 实例。

    Returns:
        str: 总结文本。章节时间轴死链删除后（#4 Q2）不再返回 chapters 字典——
        这也修复了 dict 赋给 TaskRecord.result（str 字段）的 ValidationError 隐患。
    """
    metadata = metadata or {}

    # per-task progress（支持并行 + 前端流式）
    progress = create_progress(task_id) if task_id else None
    try:
        if progress:
            progress.begin()

        video_path = Path(local_path)

        # ── 常规多模态路径 ──
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
            return _summarize_multimodal(
                Path(mp3), metadata,
                video_path=video_path,
                pre_extracted_frames=all_frames,
                progress=progress,
            )
    except Exception as e:
        logger.warning("多模态总结失败，走降级总结（仅元数据）: %s", e)
        with Timer("LLM 总结(降级)"):
            return _summarize("", metadata)
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
