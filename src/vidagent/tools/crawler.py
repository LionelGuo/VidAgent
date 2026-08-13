"""视频检索工具：三个意图明确的独立工具。

工具（Agent 使用）：
    get_hot_videos     —— 平台综合热门
    search_videos      —— 关键词搜索
    get_creator_videos —— 指定创作者（昵称或 ID，昵称自动解析）

输出统一 schema（含时长，便于 Agent 直接按时长/播放量筛选，无需下载）：
    [{video_id, title, desc, publish_time, duration, duration_text,
      video_url, platform, author, view_count}]
"""

from __future__ import annotations

import logging

from vidagent.config import settings
from vidagent.tools.platforms import get_platform
from vidagent.utils.dates import filter_today
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)

# 延迟导入：避免循环引用（platforms 模块在注册时会导入回来）
_platforms_loaded = False


def _ensure_platforms_imported() -> None:
    """确保所有平台模块已加载并注册。"""
    global _platforms_loaded
    if _platforms_loaded:
        return
    # 导入平台模块触发 register() 调用
    import vidagent.tools.platforms.bilibili     # noqa: F401
    import vidagent.tools.platforms.youtube      # noqa: F401
    import vidagent.tools.platforms.douyin       # noqa: F401
    import vidagent.tools.platforms.kuaishou     # noqa: F401
    import vidagent.tools.platforms.xiaohongshu  # noqa: F401
    _platforms_loaded = True


def _get_client(platform_name: str, **kwargs: object):
    """创建平台 HTTP 客户端，自动注入已知配置（如 B站 cookie）。"""
    p = get_platform(platform_name)
    if p.name == "bilibili" and settings.bili_cookie:
        return p.make_client(cookie=settings.bili_cookie, **kwargs)
    return p.make_client(**kwargs)


async def get_hot_videos(
    platform: str = "bilibili", limit: int = 10, date_filter: str | None = None
) -> list[dict]:
    """获取平台综合热门视频（最贴近「今日热榜」）。

    Args:
        platform: 平台名称，如 "bilibili" / "youtube"。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。

    Returns:
        每项含 video_id/title/desc/publish_time/duration/duration_text/
        video_url/platform/author/view_count。
    """
    _ensure_platforms_imported()
    p = get_platform(platform)
    with Timer(f"{p.name} 热榜"):
        async with _get_client(platform) as client:
            items = await p.get_hot(client, limit)
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]


async def search_videos(
    platform: str = "bilibili",
    keyword: str = "",
    limit: int = 10,
    date_filter: str | None = None,
) -> list[dict]:
    """按关键词搜索视频。

    Args:
        platform: 平台名称，如 "bilibili" / "youtube"。
        keyword: 搜索关键词（必填）。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。
    """
    _ensure_platforms_imported()
    if not keyword:
        raise ValueError("keyword 必填（搜索关键词）")
    p = get_platform(platform)
    with Timer(f"{p.name} 搜索"):
        async with _get_client(platform) as client:
            items = await p.search(client, keyword, limit)
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]


async def get_creator_videos(
    platform: str = "bilibili",
    creator: str = "",
    limit: int = 10,
    date_filter: str | None = None,
) -> list[dict]:
    """获取指定创作者（UP 主 / YouTuber）的视频。

    creator 可为昵称（如「老番茄」）或平台 ID。
    B站：昵称自动解析为 UID，需在 .env 配置 BILI_COOKIE。
    YouTube：需要 YOUTUBE_API_KEY 环境变量。

    Args:
        platform: 平台名称，如 "bilibili" / "youtube"。
        creator: 创作者昵称 或 平台 ID（必填）。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。
    """
    _ensure_platforms_imported()
    if not creator:
        raise ValueError("creator 必填（创作者昵称 或 ID）")
    p = get_platform(platform)
    with Timer(f"{p.name} 创作者"):
        async with _get_client(platform) as client:
            items = await p.get_creator(client, creator, limit)
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]
