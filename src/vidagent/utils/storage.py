"""工作区生命周期管理：路径、随机抖动、>7 天清理（文档 §5.1 / §5.3）。"""

from __future__ import annotations

import random
import time
from pathlib import Path

from vidagent.config import settings

CACHE_MAX_AGE_DAYS = 7
_MEDIA_EXTS = (".mp4", ".mp3", ".flv", ".webm", ".m4a")


def workspace() -> Path:
    p = Path(settings.workspace_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def sanitize(name: str) -> str:
    """转为安全文件名。Python3 的 isalnum() 对汉字/CJK 返回 True，故中文标题会被保留。"""
    keep = "-_.()"
    return "".join(c for c in name if c.isalnum() or c in keep).strip(".") or "video"


def media_path(file_name: str, ext: str) -> Path:
    """返回 workspace 下安全文件名路径；ext 含点，如 '.mp4'。"""
    return workspace() / f"{sanitize(file_name)}{ext}"


def transcript_path(video_id: str) -> Path:
    """转写文本缓存路径（按 video_id），存 workspace，享 >7 天自动清理。"""
    return workspace() / f"{sanitize(video_id)}.transcript.txt"


def random_delay(low: float = 2.0, high: float = 5.0) -> None:
    """下载前随机抖动，降低风控（文档 §5.1）。同步版。"""
    time.sleep(random.uniform(low, high))


def cleanup_old_files(max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """删除 workspace 中超过 max_age_days 的缓存媒体文件，返回删除数。任务开始前调用。"""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in workspace().iterdir():
        if f.is_file() and f.suffix.lower() in _MEDIA_EXTS and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed
