"""自适应视频帧采样：按时长决定帧数 + 均匀抽取。

参考：
- vLLM/GLM-4.6V DynamicVideoBackend：≤30s→3fps, ≤300s→1fps, >300s→0.5fps
- CVPR 2025 AKS：coverage（时间线覆盖）比 density（堆帧）更重要
- 总结场景下音频是主信息载体，帧数保守（4–16），保证覆盖面即可
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_FRAMES = 4
MAX_FRAMES = 16


def get_duration(video_path: str | Path) -> float:
    """ffprobe 取视频时长（秒），失败返回 0。"""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=10,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        logger.warning("无法解析视频时长: %s", video_path)
        return 0.0


def adaptive_frame_count(
    duration: float,
    min_frames: int = MIN_FRAMES,
    max_frames: int = MAX_FRAMES,
) -> int:
    """按时长决定关键帧数量。

    映射（参考 vLLM dynamic FPS，为总结场景调低密度）：

    | 时长      | 帧数 | 约合 fps      |
    |-----------|------|---------------|
    | ≤60s      | 6    | 0.10          |
    | ≤180s     | 8    | 0.044         |
    | ≤600s     | 10   | 0.017         |
    | ≤1800s    | 12   | 0.007         |
    | >1800s    | 16   | —             |

    保证 [min_frames, max_frames] 范围内。
    """
    if duration <= 0:
        return min_frames
    if duration <= 60:
        n = 6
    elif duration <= 180:
        n = 8
    elif duration <= 600:
        n = 10
    elif duration <= 1800:
        n = 12
    else:
        n = 16
    return max(min_frames, min(max_frames, n))


def extract_frames(
    video_path: str | Path,
    num_frames: int | None = None,
    output_dir: Path | None = None,
) -> list[Path]:
    """从视频均匀抽取关键帧（jpg），返回按时间排序的路径列表。

    Args:
        video_path: 视频文件路径。
        num_frames: 帧数。None 时自动根据时长决定（adaptive_frame_count）。
        output_dir: 输出目录。None 时创建 video_path 同目录下的 keyframes_{stem}/。

    Returns:
        jpg 文件路径列表（按时间先后排列）。失败返回空列表。
    """
    video_path = Path(video_path)
    if not video_path.exists():
        logger.warning("视频不存在，跳过帧抽取: %s", video_path)
        return []

    duration = get_duration(video_path)
    if duration <= 0:
        logger.warning("视频时长为 0，跳过帧抽取")
        return []

    if num_frames is None:
        num_frames = adaptive_frame_count(duration)

    if output_dir is None:
        output_dir = video_path.parent / f"keyframes_{video_path.stem}"
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[Path] = []
    interval = duration / (num_frames + 1)

    for i in range(1, num_frames + 1):
        t = interval * i
        out_path = output_dir / f"frame_{i:02d}_{int(t)}s.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(out_path)],
            capture_output=True, timeout=15,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            frames.append(out_path)

    logger.info("帧抽取完成：%d/%d 帧（视频 %s，时长 %.0fs）",
                len(frames), num_frames, video_path.name, duration)
    return frames
