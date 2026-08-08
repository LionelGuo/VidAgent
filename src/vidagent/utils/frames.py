"""自适应视频帧采样：按时长决定帧数 + 并行 seek 抽取。

参考：
- vLLM/GLM-4.6V DynamicVideoBackend：≤30s→3fps, ≤300s→1fps, >300s→0.5fps
- CVPR 2025 AKS：coverage（时间线覆盖）比 density（堆帧）更重要
- 总结场景下音频是主信息载体，帧数保守（4–16），保证覆盖面即可

v2 优化：
- N 次独立 ffmpeg seek → ThreadPoolExecutor 并行执行，~3-5x 提速
- 帧缓存：已存在的 keyframes 目录直接复用
- 可传入已知 duration（来自爬虫元数据），省一次 ffprobe
- 图片缩放至 512px 宽度（模型内部进一步下采样，全分辨率无意义）
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _extract_one_frame(
    video_path: Path, timestamp: float, index: int, output_dir: Path,
) -> Path | None:
    """抽取单个时间点的帧（在 ThreadPoolExecutor 中并行调用）。"""
    out_path = output_dir / f"frame_{index:02d}_{int(timestamp)}s.jpg"
    # -ss 放在 -i 前 = input seeking（快速，跳到最近关键帧后解码）
    # scale=-2:512 = 宽度 512px，高度按比例
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(video_path),
         "-frames:v", "1", "-vf", "scale=-2:512", "-q:v", "5",
         str(out_path)],
        capture_output=True, timeout=15,
    )
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    return None


def extract_frames(
    video_path: str | Path,
    num_frames: int | None = None,
    output_dir: Path | None = None,
    duration: float | None = None,
) -> list[Path]:
    """从视频均匀抽取关键帧（jpg），返回按时间排序的路径列表。

    Args:
        video_path: 视频文件路径。
        num_frames: 帧数。None 时自动根据时长决定（adaptive_frame_count）。
        output_dir: 输出目录。None 时创建 video_path 同目录下的 keyframes_{stem}/。
        duration: 视频时长（秒）。已知时可传入，跳过 ffprobe。

    Returns:
        jpg 文件路径列表（按时间先后排列）。失败返回空列表。
    """
    video_path = Path(video_path)
    if not video_path.exists():
        logger.warning("视频不存在，跳过帧抽取: %s", video_path)
        return []

    # ── 帧缓存：目录已存在且帧数足够 → 直接复用 ──
    if output_dir is None:
        output_dir = video_path.parent / f"keyframes_{video_path.stem}"

    if output_dir.exists():
        existing = sorted(output_dir.glob("frame_*.jpg"))
        if len(existing) >= MIN_FRAMES:
            logger.info("帧缓存命中：%d 帧 → %s", len(existing), output_dir)
            return existing

    # ── 时长 ──
    if duration is None:
        duration = get_duration(video_path)
    if duration <= 0:
        logger.warning("视频时长为 0，跳过帧抽取")
        return []

    if num_frames is None:
        num_frames = adaptive_frame_count(duration)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 并行 seek：N 次独立 ffmpeg 在 ThreadPool 中并发 ──
    interval = duration / (num_frames + 1)
    frames: list[Path] = []

    with ThreadPoolExecutor(max_workers=min(num_frames, 6)) as pool:
        futures = {}
        for i in range(1, num_frames + 1):
            t = interval * i
            fut = pool.submit(_extract_one_frame, video_path, t, i, output_dir)
            futures[fut] = i

        for fut in as_completed(futures):
            result = fut.result()
            if result:
                frames.append(result)

    frames.sort()
    logger.info(
        "帧抽取完成：%d/%d 帧（视频 %s，时长 %.0fs，并行 seek）",
        len(frames), num_frames, video_path.name, duration,
    )
    return frames
