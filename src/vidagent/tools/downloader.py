"""Tool 2: download_video —— 无水印视频下载（按平台分流）。

使用平台注册表 detect_platform() → platform.download() 自动路由。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from vidagent.tools.platforms import detect_platform
from vidagent.utils import storage

logger = logging.getLogger(__name__)

# 确保平台已注册
_platforms_loaded = False


def _ensure_platforms() -> None:
    global _platforms_loaded
    if _platforms_loaded:
        return
    import vidagent.tools.platforms.bilibili  # noqa: F401
    import vidagent.tools.platforms.douyin  # noqa: F401
    import vidagent.tools.platforms.kuaishou  # noqa: F401
    import vidagent.tools.platforms.xiaohongshu  # noqa: F401
    import vidagent.tools.platforms.youtube  # noqa: F401
    _platforms_loaded = True


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
