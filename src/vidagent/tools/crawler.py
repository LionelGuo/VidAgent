"""视频检索工具：三个意图明确的独立工具 + 一个向后兼容的分发器。

工具（Agent 使用）：
    get_hot_videos     —— 平台综合热门
    search_videos      —— 关键词搜索
    get_creator_videos —— 指定创作者（昵称或 UID，昵称自动解析）

输出统一 schema（含时长，便于 Agent 直接按时长/播放量筛选，无需下载）：
    [{video_id, title, desc, publish_time, duration, duration_text,
      video_url, platform, author, view_count}]

平台：bilibili（已实现）；抖音/小红书/快手（Sprint4 经 MediaCrawler 接入）。
"""

from __future__ import annotations

import logging

from vidagent.config import settings
from vidagent.tools import bilibili, hotboard
from vidagent.utils.dates import filter_today
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)

_BILI_ALIASES = ("bilibili", "bili", "b站")


def _ensure_bili(platform: str) -> str:
    p = platform.lower()
    if p not in _BILI_ALIASES:
        raise NotImplementedError(f"平台暂未接入: {platform}（Sprint4 计划接入抖音/小红书/快手）")
    return p


async def get_hot_videos(
    platform: str = "bilibili", limit: int = 10, date_filter: str | None = None
) -> list[dict]:
    """获取平台综合热门视频（最贴近「今日热榜」）。

    Args:
        platform: 平台，目前支持 "bilibili"。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。

    Returns:
        每项含 video_id/title/desc/publish_time/duration/duration_text/
        video_url/platform/author/view_count。
    """
    _ensure_bili(platform)
    with Timer("B站抓取"):
        async with bilibili.make_client(cookie=settings.bili_cookie or None) as client:
            return await hotboard.fetch_hot_board(client, "bilibili", date_filter, limit)


async def search_videos(
    platform: str = "bilibili",
    keyword: str = "",
    limit: int = 10,
    date_filter: str | None = None,
) -> list[dict]:
    """按关键词搜索视频。

    Args:
        platform: 平台，目前支持 "bilibili"。
        keyword: 搜索关键词（必填）。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。
    """
    _ensure_bili(platform)
    if not keyword:
        raise ValueError("keyword 必填（搜索关键词）")
    with Timer("B站抓取"):
        async with bilibili.make_client(cookie=settings.bili_cookie or None) as client:
            items = await bilibili.search_videos(client, keyword, page_size=max(limit, 20))
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]


async def get_creator_videos(
    platform: str = "bilibili",
    creator: str = "",
    limit: int = 10,
    date_filter: str | None = None,
) -> list[dict]:
    """获取指定创作者（UP 主）的视频。

    creator 可为昵称（如「老番茄」，自动解析为 UID）或数字 UID。
    注意：B站该接口风控较严，需在 .env 配置 BILI_COOKIE（含 SESSDATA）。

    Args:
        platform: 平台，目前支持 "bilibili"。
        creator: 创作者昵称 或 数字 UID（必填）。
        limit: 返回条数上限。
        date_filter: 时间过滤，目前支持 "today"。
    """
    _ensure_bili(platform)
    if not creator:
        raise ValueError("creator 必填（创作者昵称 或 UID）")
    with Timer("B站抓取"):
        async with bilibili.make_client(cookie=settings.bili_cookie or None) as client:
            mid = str(creator)
            if not mid.isdigit():
                mid, uname, fans = await bilibili.resolve_creator_mid(client, mid)
                logger.info("创作者「%s」解析为 mid=%s（%s，粉丝 %s）", creator, mid, uname, fans)
            items = await bilibili.fetch_user_videos(client, mid, ps=max(limit, 30))
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]


async def search_and_fetch_videos(
    platform: str,
    task_type: str,  # hot_board | search | user_homepage
    target_id: str | None = None,
    date_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """[向后兼容] 按 task_type 分派到上面三个工具。供 pipeline / crawl_cli / 旧测试使用。"""
    if task_type == "hot_board":
        return await get_hot_videos(platform, limit, date_filter)
    if task_type == "search":
        return await search_videos(platform, target_id or "", limit, date_filter)
    if task_type == "user_homepage":
        return await get_creator_videos(platform, target_id or "", limit, date_filter)
    raise ValueError(f"未知 task_type: {task_type}（可选: hot_board / search / user_homepage）")
