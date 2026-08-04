"""自建热榜抓取器：按平台获取当前热门/排行榜视频。

MediaCrawler 原生无「热榜」类型（仅 search/detail/creator），故按平台自建。
B站优先实现（综合热门 popular 最贴近「今日热榜」）。
"""

from __future__ import annotations

import httpx

from vidagent.tools import bilibili
from vidagent.utils.dates import filter_today


async def fetch_hot_board(
    client: httpx.AsyncClient,
    platform: str,
    date_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    platform = platform.lower()
    if platform in ("bilibili", "bili", "b站"):
        # 综合热门最贴近「今日热榜」；多取一些以便 date_filter 后仍有足够数量
        items = await bilibili.fetch_popular(client, ps=max(limit, 20))
    else:
        raise NotImplementedError(
            f"热榜暂未实现平台: {platform}（计划 Sprint4 经 MediaCrawler 接入抖音/小红书）"
        )

    if date_filter == "today":
        items = filter_today(items)
    return items[:limit]
