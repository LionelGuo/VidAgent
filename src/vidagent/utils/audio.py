"""音频提取：基于 ffmpeg 子进程（比 moviepy 轻；系统已装 ffmpeg 6.1.1）。

无音频轨的视频会让 ffmpeg 返回非 0——交由上层（summarizer）做降级总结（文档 §5.2）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from vidagent.utils import storage


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def extract_audio(video_path: str | Path, mp3_name: str | None = None) -> Path:
    """从视频提取音频为 mp3，返回 mp3 路径。无音频轨会抛 RuntimeError（上层降级）。"""
    if not ffmpeg_available():
        raise RuntimeError("未找到 ffmpeg，请先安装 ffmpeg")

    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    base = storage.sanitize(mp3_name or video_path.stem)
    out = storage.media_path(base, ".mp3")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "libmp3lame", "-q:a", "4", str(out),
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not out.exists():
        tail = res.stderr.decode(errors="ignore")[-400:]
        raise RuntimeError(f"ffmpeg 抽音失败（可能无音频轨）: {tail}")
    return out
