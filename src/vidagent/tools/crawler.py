"""Tool 1: search_and_fetch_videos —— 按平台/任务类型获取视频元数据。

输出统一 schema：
    [{video_id, title, desc, publish_time, video_url, platform, author, view_count}]

平台支持：
    bilibili（已实现）：hot_board / search / user_homepage
    douyin / xiaohongshu / kuaishou（Sprint4 经 MediaCrawler 接入）
"""

from __future__ import annotations

from vidagent.config import settings
from vidagent.tools import bilibili, hotboard
from vidagent.utils.dates import filter_today

_BILI_ALIASES = ("bilibili", "bili", "b站")


async def search_and_fetch_videos(
    platform: str,
    task_type: str,  # hot_board | search | user_homepage
    target_id: str | None = None,  # 关键词 或 用户 UID/mid
    date_filter: str | None = None,  # "today"
    limit: int = 10,
) -> list[dict]:
    """获取视频元数据列表（按平台/任务类型）。

    Args:
        platform: 平台，目前支持 "bilibili"。
        task_type: 任务类型："hot_board"(平台综合热门) / "search"(关键词搜索) /
            "user_homepage"(指定创作者主页，B站需登录 Cookie)。
        target_id: search 时为搜索关键词；user_homepage 时为用户 UID/mid；hot_board 时留空。
        date_filter: 时间过滤，目前支持 "today"（仅今天发布；过滤为空则回退原列表）。
        limit: 返回条数上限。

    Returns:
        每项含 video_id / title / desc / publish_time / video_url / platform / author / view_count。
    """
    platform = platform.lower()
    if platform in _BILI_ALIASES:
        return await _bilibili(task_type, target_id, date_filter, limit)
    raise NotImplementedError(f"平台暂未接入: {platform}（Sprint4 计划接入抖音/小红书/快手）")


async def _bilibili(
    task_type: str, target_id: str | None, date_filter: str | None, limit: int
) -> list[dict]:
    async with bilibili.make_client(cookie=settings.bili_cookie or None) as client:
        if task_type == "hot_board":
            return await hotboard.fetch_hot_board(client, "bilibili", date_filter, limit)

        if task_type == "search":
            if not target_id:
                raise ValueError("task_type=search 时 target_id 必填（搜索关键词）")
            items = await bilibili.search_videos(client, target_id, page_size=max(limit, 20))
        elif task_type == "user_homepage":
            if not target_id:
                raise ValueError("task_type=user_homepage 时 target_id 必填（用户 UID/mid）")
            items = await bilibili.fetch_user_videos(client, target_id, ps=max(limit, 30))
        else:
            raise ValueError(
                f"未知 task_type: {task_type}（可选: hot_board / search / user_homepage）"
            )

    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]
