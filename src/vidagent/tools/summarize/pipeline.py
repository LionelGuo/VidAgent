"""总结管线公开入口与降级路径（#4 深模块：自原 summarizer.py 拆出）。

extract_and_summarize 是总结域唯一公开入口：90s 分流（短视频视频 base64 /
长视频音频+帧）→ 多模态总结；失败降级为仅元数据总结（不崩溃）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from vidagent import llm_provider
from vidagent.tools.summarize.multimodal import _summarize_multimodal
from vidagent.tools.summarize.progress import create_progress, get_progress
from vidagent.tools.summarize.prompts import _SUMMARY_SYS
from vidagent.tools.summarize.short_video import _summarize_short_video
from vidagent.tools.summarize.transport import _chat_completion
from vidagent.utils.audio import extract_audio
from vidagent.utils.frames import extract_frames, get_duration
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)


def extract_and_summarize(
    local_path: str,
    metadata: dict | None = None,
    task_id: str | None = None,
) -> str:
    """对本地视频生成结构化中文总结（Markdown）。

    总结域唯一公开入口（#4 Q3 深模块）：内化 <90s 短视频 / 长视频分流、
    时长补充、progress 管理。单视频端点与批量端点共用此入口——
    单端点 <90s 视频自此改走视频 base64 短管线（对齐 CONTEXT.md 设计）。

    无音频轨/任何失败时自动降级为仅依据元数据的总结（不报错）。

    Args:
        local_path: 本地视频文件路径（用 download_video 返回的 local_path）。
        metadata: 视频元数据，至少含 title 与 desc。
        task_id: 可选，per-task 进度追踪 ID。批量端点下载阶段已预创建
            （推送下载进度），此处 get-or-create 复用。

    Returns:
        str: 总结文本。章节时间轴死链删除后（#4 Q2）不再返回 chapters 字典——
        这也修复了 dict 赋给 TaskRecord.result（str 字段）的 ValidationError 隐患。
    """
    metadata = metadata or {}

    # per-task progress（支持并行 + 前端流式）：批量端点已预创建则复用
    progress = None
    if task_id:
        progress = get_progress(task_id) or create_progress(task_id)

    try:
        if progress:
            progress.begin()

        video_path = Path(local_path)

        # ── 时长：缺失时从本地文件 ffprobe 补充（热搜/搜索结果常缺 duration）──
        duration = metadata.get("duration") or 0
        if not duration:
            duration = get_duration(local_path)
            metadata["duration"] = duration
            metadata["duration_text"] = f"{int(duration // 60):02d}:{int(duration % 60):02d}"
            logger.info("📐 补充 duration: %.0fs", duration)

        # ── 分流：短视频（<90s）vs 长视频 ──
        if isinstance(duration, (int, float)) and 0 < duration < 90:
            logger.info("🎬 短视频管线: %.0fs", duration)
            _ep = llm_provider.agent_endpoint()
            with Timer("多模态总结(短视频)"):
                return _summarize_short_video(
                    video_path=video_path,
                    metadata=metadata,
                    base_url=_ep.base_url, api_key=_ep.api_key, model=_ep.model,
                    progress=progress,
                )

        # ── 长视频：并行音频提取 + 均匀帧抽取 → 流式总结（超长音频自动分块）──
        from concurrent.futures import ThreadPoolExecutor

        t0_pre = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            audio_future = pool.submit(extract_audio, local_path)
            frames_future = pool.submit(
                extract_frames, video_path,
                duration=duration,
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
