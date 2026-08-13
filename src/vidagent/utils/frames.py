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
- extract_frames() 支持 timestamps 参数，按指定时间点抽帧
"""

from __future__ import annotations

import logging
import subprocess
import time
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
    timestamps: list[float] | None = None,
) -> list[Path]:
    """从视频抽取关键帧（jpg），返回按时间排序的路径列表。

    Args:
        video_path: 视频文件路径。
        num_frames: 帧数。None 时自动根据时长决定（adaptive_frame_count）。
                    仅在 timestamps=None 时生效。
        output_dir: 输出目录。None 时创建 video_path 同目录下的 keyframes_{stem}/。
        duration: 视频时长（秒）。已知时可传入，跳过 ffprobe。
        timestamps: 指定抽帧时间点（秒）。传入时 num_frames 参数被忽略，
                    在每个时间点各抽一帧。不传入时按均匀采样。

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
        need = len(timestamps) if timestamps else (num_frames or adaptive_frame_count(duration or 60))
        if len(existing) >= need:
            total_kb = sum(f.stat().st_size for f in existing) // 1024
            logger.info(
                "🖼️ 帧缓存命中: %d 帧 / %d KB → %s",
                len(existing), total_kb, output_dir.name,
            )
            return existing
        else:
            logger.info("🖼️ 帧缓存过期: 需要 %d 帧, 已有 %d → 重新抽取", need, len(existing))

    # ── 时长 ──
    if duration is None:
        t0_dur = time.perf_counter()
        duration = get_duration(video_path)
        logger.debug("ffprobe 时长: %.1fs (%.2fs)", duration, time.perf_counter() - t0_dur)
    if duration <= 0:
        logger.warning("视频时长为 0，跳过帧抽取")
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 确定要抽帧的时间点 ──
    if timestamps is not None:
        # 按指定时间戳抽帧
        points = [(i, t) for i, t in enumerate(timestamps, 1)]
        num_to_extract = len(points)
    else:
        # 均匀采样
        if num_frames is None:
            num_frames = adaptive_frame_count(duration)
        interval = duration / (num_frames + 1)
        points = [(i, interval * i) for i in range(1, num_frames + 1)]
        num_to_extract = num_frames

    # ── 并行 seek：N 次独立 ffmpeg 在 ThreadPool 中并发 ──
    frames: list[Path] = []
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=min(num_to_extract, 6)) as pool:
        futures = {}
        for idx, timestamp in points:
            fut = pool.submit(_extract_one_frame, video_path, timestamp, idx, output_dir)
            futures[fut] = idx

        for fut in as_completed(futures):
            result = fut.result()
            if result:
                frames.append(result)

    elapsed = time.perf_counter() - t0
    frames.sort()
    total_kb = sum(f.stat().st_size for f in frames) // 1024
    logger.info(
        "🖼️ 帧抽取完成: %d/%d 帧 / %d KB / %.1fs (视频 %s, %.0fs, %d workers)",
        len(frames), num_to_extract, total_kb, elapsed,
        video_path.name, duration, min(num_to_extract, 6),
    )
    return frames
