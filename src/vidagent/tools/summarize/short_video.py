"""短视频管线（#4 深模块：自原 summarizer.py 拆出）。

<90s 视频：转码小体积无音轨视频 + 独立音频 → base64 video_url + 音频
→ 单次 LLM 调用（短视频专用 prompt，细粒度不遗漏细节）。
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vidagent import llm_provider
from vidagent.tools.summarize.progress import Progress
from vidagent.tools.summarize.prompts import _SUMMARY_SYS_SHORT, build_meta_block
from vidagent.tools.summarize.transport import _chat_completion_stream

logger = logging.getLogger(__name__)


def _prepare_short_video(video_path: Path) -> tuple[Path, Path]:
    """预处理短视频：转码 H.264、缩分辨率、降帧率、剥离音频。

    转码与音频提取并行执行（两者输出独立、互不依赖，各省一次 ffmpeg 等待）。

    Returns:
        (processed_video_path, audio_path) — 小体积无音轨视频 + 独立音频
    """
    from vidagent.utils.audio import extract_audio as _extract_audio

    work = Path(tempfile.mkdtemp(prefix="vidagent_short_"))
    processed = work / "video.mp4"

    def _transcode() -> Path:
        # 384px 宽, 4fps, H.264, 无音轨
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
        return processed

    # 并行：转码 ∥ 音频提取（音频输出到 workspace，复用缓存）
    with ThreadPoolExecutor(max_workers=2) as pool:
        transcode_future = pool.submit(_transcode)
        audio_future = pool.submit(_extract_audio, str(video_path))
        transcode_future.result()
        audio_result = audio_future.result()

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
    progress: Progress | None = None,
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
    meta_block = build_meta_block(metadata)

    content_parts: list[dict] = [
        {"type": "text", "text": f"{meta_block}\n请仔细分析这个短视频的音频和画面，输出精准详细的总结。"},
        llm_provider.build_audio_part(mp3_b64),
        llm_provider.build_video_part(video_b64),
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
