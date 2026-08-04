"""Tool 2: download_video —— 无水印视频下载（按平台分流）。

B站：yt-dlp（B站本身无水印，最稳）
抖音/小红书/快手：f2（Sprint4 接入）
"""

from __future__ import annotations

from pathlib import Path

import yt_dlp

from vidagent.utils import storage


def _platform_of(url: str) -> str:
    u = url.lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "douyin.com" in u:
        return "douyin"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "xiaohongshu"
    if "kuaishou.com" in u or "chenzhongtech.com" in u:
        return "kuaishou"
    return "unknown"


def download_video(video_url: str, file_name: str) -> dict:
    """下载视频到 workspace。

    返回 {"status": "success", "local_path": ..., "platform": ...}
    或    {"status": "error", "error": ..., "video_url": ...}（交由 Agent 反思重试）。
    """
    platform = _platform_of(video_url)
    if platform == "bilibili":
        return _download_bili(video_url, file_name)
    raise NotImplementedError(
        f"下载暂未实现平台: {platform}（Sprint4 经 f2 接入抖音/小红书/快手）"
    )


def _download_bili(url: str, file_name: str) -> dict:
    storage.random_delay()  # 随机抖动降风控（文档 §5.1）
    target = storage.media_path(file_name, ".mp4")  # 仅用于确定命名前缀
    opts = {
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        return {"status": "error", "error": f"yt-dlp 下载失败: {e}", "video_url": url}

    local = _find_result(storage.sanitize(file_name))
    if not local:
        return {"status": "error", "error": "下载完成但未找到产物文件", "video_url": url}
    return {"status": "success", "local_path": str(local), "platform": "bilibili"}


def _find_result(base_name: str) -> Path | None:
    ws = storage.workspace()
    # yt-dlp 合并产物为 {base}.mp4
    cand = ws / f"{base_name}.mp4"
    if cand.exists():
        return cand
    # 兜底：取最新 .mp4
    mp4s = sorted(ws.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[0] if mp4s else None
