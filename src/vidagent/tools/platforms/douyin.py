"""抖音平台适配：公开热榜 API + f2 下载 + MediaCrawler 搜索（分期实施）。

P0 (当前): 公开热榜 API — 无需登录、无需 API Key
P1 (后续): f2 下载 + MediaCrawler 搜索
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from vidagent.config import settings
from vidagent.tools.platforms import Platform, register

logger = logging.getLogger(__name__)

# 抖音热榜公开 API（无需登录）
_HOT_SEARCH_API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}

# 抖音视频 ID：19 位数字，或短链 v.douyin.com/xxx
_DY_VIDEO_ID_RE = re.compile(r"/video/(\d{15,20})")
_DY_SHORT_RE = re.compile(r"v\.douyin\.com/(\w+)")


def _get_proxy() -> str | None:
    """复用 YouTube 代理设置（抖音也需要科学上网或国内代理）。"""
    p = settings.youtube_proxy
    return p if p else None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    """从抖音 URL 提取视频 ID。

    支持格式:
    - https://www.douyin.com/video/7123456789012345678
    - https://v.douyin.com/xxxxx/ （短链，需重定向解析）
    """
    m = _DY_VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    # 短链：返回短链标识，实际解析在下载时由 f2 完成
    m2 = _DY_SHORT_RE.search(url)
    if m2:
        return f"dy_short_{m2.group(1)}"
    return None


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """创建抖音 HTTP 客户端。"""
    proxy = _get_proxy()
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        proxy=proxy,
    )


def normalize(item: dict) -> dict:
    """抖音数据 → 统一 schema。

    支持两种来源:
    - 热榜词条 (trending topic): word, hot_value, video_count, word_cover
    - 视频详情 (video detail): aweme_id, desc, author, statistics (P1 实现)
    """
    # 判断数据来源
    if "word" in item and "hot_value" in item:
        return _normalize_trending(item)
    return _normalize_video(item)


def _normalize_trending(item: dict) -> dict:
    """热榜词条 → 统一 schema。

    热榜返回的是热搜话题，不是具体视频。将其映射为可搜索的条目，
    Agent 后续可通过 search_videos(keyword=word) 获取该话题下的视频。
    """
    word = item.get("word", "")
    video_count = item.get("video_count", 0)
    hot_value = item.get("hot_value", 0)
    event_time = item.get("event_time", 0)
    group_id = item.get("group_id", "")

    # 封面图
    cover = item.get("word_cover", {})
    cover_url = ""
    if isinstance(cover, dict):
        urls = cover.get("url_list", [])
        cover_url = urls[0] if urls else ""

    return {
        "video_id": group_id or f"dy_trend_{hash(word) & 0xFFFFFFFF:08x}",
        "title": word,
        "desc": f"抖音热搜 · 热度 {hot_value} · {video_count} 个视频",
        "publish_time": event_time,
        "duration": 0,  # 热搜词条无时长
        "duration_text": "",
        "video_url": f"https://www.douyin.com/search/{word}" if word else "",
        "platform": "douyin",
        "author": "",
        "view_count": video_count,  # 相关视频数
        # 额外信息（非标准字段，供 Agent 参考）
        "hot_value": hot_value,
        "cover_url": cover_url,
        "is_trending_topic": True,  # 标记：这是热搜词条，不是具体视频
    }


def _normalize_video(item: dict) -> dict:
    """视频详情 → 统一 schema（P1 实现）。"""
    # 抖音视频字段映射（占位，P1 接入 f2 后补充完整映射）
    aweme_id = item.get("aweme_id") or item.get("video_id", "")
    desc_text = item.get("desc") or item.get("title", "")
    author_info = item.get("author") or {}
    author_name = author_info.get("nickname", "") if isinstance(author_info, dict) else ""
    stats = item.get("statistics") or {}
    duration_ms = item.get("duration", 0)  # 毫秒
    duration_sec = int(duration_ms / 1000) if duration_ms > 1000 else int(duration_ms)
    create_time = item.get("create_time", 0)

    return {
        "video_id": str(aweme_id),
        "title": desc_text[:200] if desc_text else "",
        "desc": desc_text[:500] if desc_text else "",
        "publish_time": int(create_time),
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec),
        "video_url": item.get("share_url", "") or f"https://www.douyin.com/video/{aweme_id}",
        "platform": "douyin",
        "author": author_name,
        "view_count": int(stats.get("digg_count", 0)),  # 抖音用点赞数近似
    }


def _fmt_duration(sec: int) -> str:
    """秒 → 'MM:SS'。"""
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 热榜（P0 — 已实现）
# ---------------------------------------------------------------------------

async def fetch_hot_search(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
    """获取抖音实时热搜榜。

    公开 API，无需登录/API Key。返回热搜词条列表。
    """
    try:
        resp = await client.get(_HOT_SEARCH_API)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("抖音热榜 API 请求失败: %s", e)
        return []
    except ValueError as e:
        logger.warning("抖音热榜 API 返回非 JSON: %s", e)
        return []

    trending = data.get("data", {}).get("trending_list", [])
    active_time = data.get("data", {}).get("active_time", "")
    logger.info("🔥 抖音热榜: %d 条 (更新时间: %s)", len(trending), active_time)

    results = [normalize(it) for it in trending[:limit]]
    return results


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class DouyinPlatform(Platform):
    name: ClassVar[str] = "douyin"
    aliases: ClassVar[tuple[str, ...]] = ("dy", "抖音")

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return extract_video_id(url)

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
        return make_client(timeout)

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
        """抖音实时热搜榜。

        注意：返回的是热搜**词条**，不是具体视频列表。
        Agent 可据此了解当前热门话题，后续通过 search() 获取话题下的视频。
        """
        return await fetch_hot_search(client, limit)

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索视频（P1 实现，需 MediaCrawler CDP）。"""
        raise NotImplementedError(
            "抖音搜索需 MediaCrawler 支持（P1 计划）。当前可用: 热榜 (get_hot)"
        )

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频（P1 实现，需 MediaCrawler 或 f2）。"""
        raise NotImplementedError(
            "抖音创作者查询需 MediaCrawler 或 f2 支持（P1 计划）。当前可用: 热榜 (get_hot)"
        )

    @staticmethod
    def download(video_url: str, file_name: str) -> dict:
        """下载抖音视频（P1 实现，需 f2）。"""
        raise NotImplementedError(
            "抖音下载需 f2 支持（P1 计划）。当前可用: 热榜 (get_hot)"
        )


# 注册到全局注册表
register(DouyinPlatform)
