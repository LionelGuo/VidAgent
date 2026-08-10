"""热榜抓取器：按平台获取当前热门/排行榜视频。

使用平台注册表自动路由到对应平台的 get_hot() 实现。
"""

from __future__ import annotations

import httpx

from vidagent.tools.platforms import get_platform
from vidagent.utils.dates import filter_today

_platforms_loaded = False


def _ensure_platforms() -> None:
    global _platforms_loaded
    if _platforms_loaded:
        return
    import vidagent.tools.platforms.bilibili     # noqa: F401
    import vidagent.tools.platforms.youtube      # noqa: F401
    import vidagent.tools.platforms.douyin       # noqa: F401
    import vidagent.tools.platforms.kuaishou     # noqa: F401
    import vidagent.tools.platforms.xiaohongshu  # noqa: F401
    _platforms_loaded = True


async def fetch_hot_board(
    client: httpx.AsyncClient,
    platform: str,
    date_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """按平台获取热门视频榜单。

    Args:
        client: 平台 HTTP 客户端（已配置认证/Cookie）。
        platform: 平台名称。
        date_filter: 时间过滤，支持 "today"。
        limit: 返回条数上限。
    """
    _ensure_platforms()
    p = get_platform(platform)
    items = await p.get_hot(client, limit)
    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]
