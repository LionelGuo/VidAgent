"""Tool 2: download_video —— 无水印视频下载（按平台分流）。

使用平台注册表 detect_platform() → platform.download() 自动路由。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

# 确保平台已注册（清单单一来源在 platforms/__init__.py，#3 Q6）：
# 别名保留原私有名，避免改动调用点
from vidagent.tools.platforms import (
    detect_platform,
)
from vidagent.tools.platforms import (
    ensure_platforms_imported as _ensure_platforms,
)
from vidagent.utils import storage

logger = logging.getLogger(__name__)

# 批量管线下载重试策略（#4 Q3：自 server/main.py 移入 downloader 域）
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2  # 秒，指数退避：2s / 4s / 8s / 16s


def _platform_of(url: str) -> str:
    """从 URL 检测平台名称（向后兼容，供测试使用）。"""
    _ensure_platforms()
    p = detect_platform(url)
    return p.name if p else "unknown"


def download_video(video_url: str, file_name: str,
                   progress_callback: Callable[[int], None] | None = None) -> dict:
    """下载视频（无水印）到本地 workspace 目录。

    Args:
        video_url: 视频地址（用检索工具返回的 video_url）。
        file_name: 保存文件名前缀，通常用 video_id。
        progress_callback: 下载进度回调，参数为 0-100 的百分比整数。

    Returns:
        {"status":"success","local_path":...,"platform":...,"cached":bool}，
        或失败时 {"status":"error","error":...,"video_url":...}（应反思重试）。
    """
    _ensure_platforms()

    # 下载缓存：已存在则直接复用，跳过下载
    target = storage.media_path(file_name, ".mp4")
    if target.exists():
        logger.info("下载命中缓存: %s", target)
        if progress_callback:
            progress_callback(100)
        platform_name = detect_platform(video_url)
        return {
            "status": "success",
            "local_path": str(target),
            "platform": platform_name.name if platform_name else "unknown",
            "cached": True,
        }

    platform = detect_platform(video_url)
    if platform is None:
        raise NotImplementedError(
            f"无法识别平台（URL: {video_url}）。当前支持: bilibili / youtube"
        )

    storage.random_delay()  # 随机抖动降风控
    return platform.download(video_url, file_name, progress_callback=progress_callback)


def download_video_with_retry(
    video_url: str,
    video_id: str,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[str | None, str | None]:
    """带指数退避重试的下载（批量总结管线用；#4 Q3 自 server/main.py 移入）。

    单次下载工具仍用 download_video（无重试，行为不变）。

    Returns:
        (local_path, last_error)：成功时 error 为 None；失败（含 fatal 短路、
        重试耗尽）时 path 为 None。
    """
    last_err: str | None = None

    for retry in range(1, MAX_RETRIES + 1):
        try:
            result = download_video(video_url, video_id, progress_callback=progress_callback)
            if result.get("status") == "success":
                return result["local_path"], None
            last_err = result.get("error", "未知下载错误")
            if result.get("fatal"):
                # 确定性业务错误（如小红书图文笔记），重试无意义
                break
        except Exception as e:
            last_err = str(e)
        if retry < MAX_RETRIES:
            delay = RETRY_BASE_DELAY ** retry
            logger.warning(
                "下载重试 %d/%d (%.0fs 后退避): %s",
                retry, MAX_RETRIES, delay, video_id,
            )
            time.sleep(delay)

    return None, last_err
