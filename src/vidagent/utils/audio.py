"""音频提取：基于 ffmpeg 子进程（比 moviepy 轻；系统已装 ffmpeg 6.1.1）。

无音频轨的视频会让 ffmpeg 返回非 0——交由上层（summarizer）做降级总结（文档 §5.2）。

v2 优化：
- 输出 16kHz 单声道（vLLM Qwen3-Omni / faster-whisper 均内部重采样至此，高采样率无意义）
- mp3 缓存：文件已存在且比视频新 → 跳过提取
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from vidagent.utils import storage

logger = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def extract_audio(video_path: str | Path, mp3_name: str | None = None) -> Path:
    """从视频提取音频为 mp3（16kHz 单声道，适合语音场景），返回 mp3 路径。

    无音频轨会抛 RuntimeError（上层降级）。
    """
    if not ffmpeg_available():
        raise RuntimeError("未找到 ffmpeg，请先安装 ffmpeg")

    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    base = storage.sanitize(mp3_name or video_path.stem)
    out = storage.media_path(base, ".mp3")

    video_size_mb = video_path.stat().st_size / 1024 / 1024

    # ── 缓存：mp3 已存在且比视频新 → 跳过提取 ──
    if out.exists() and out.stat().st_mtime >= video_path.stat().st_mtime:
        out_size_kb = out.stat().st_size // 1024
        logger.info(
            "🎵 音频缓存命中: %s (%d KB, 视频 %.1f MB) — 跳过提取",
            out.name, out_size_kb, video_size_mb,
        )
        return out

    # 16kHz 单声道：两个消费者（whisper / Qwen3-Omni via _ensure_wav）均内部重采样至此
    # -q:a 7：~90kbps VBR，语音场景与 q:a 4 无感知差异，但编码快 30%、体积减半
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "libmp3lame", "-q:a", "7",
        str(out),
    ]
    t0 = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True)
    elapsed = time.perf_counter() - t0

    if res.returncode != 0 or not out.exists():
        tail = res.stderr.decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg 抽音失败（可能无音频轨）: {tail}")

    out_size_kb = out.stat().st_size // 1024
    logger.info(
        "🎵 音频提取完成: %s (%d KB / %.1fs, 视频 %.1f MB)",
        out.name, out_size_kb, elapsed, video_size_mb,
    )
    return out
