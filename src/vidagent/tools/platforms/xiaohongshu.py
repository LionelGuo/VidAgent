"""小红书平台适配：MediaCrawler CDP 搜索 + 创作者 + 下载。

使用 XiaoHongShuClient (REST API + xhshow 签名)，与抖音共享浏览器管理模式。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, ClassVar

import httpx
from playwright.async_api import async_playwright, BrowserContext, Page

from vidagent.config import settings
from vidagent.tools.platforms import Platform, register

logger = logging.getLogger(__name__)

_WORKSPACE = Path(settings.workspace_dir).resolve()

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.xiaohongshu.com/",
}

_XHS_NOTE_RE = re.compile(r"/explore/(\w+)|/discovery/item/(\w+)")


def _get_proxy() -> str | None:
    return settings.youtube_proxy or None


# ---------------------------------------------------------------------------
# 客户端管理（通过共享 CDP 连接 Windows Chrome）
# ---------------------------------------------------------------------------

_client = None
_client_initialized = False


_client = None
_client_initialized = False


async def _ensure_client():
    global _client, _client_initialized

    if _client_initialized and _client is not None:
        return _client

    from ._cdp_browser import get_page_for_platform, chdir_mc

    os.chdir(chdir_mc())
    try:
        import config as mc_config
        mc_config.PLATFORM = "xhs"
        mc_config.ENABLE_GET_MEIDAS = False
        mc_config.ENABLE_GET_COMMENTS = False

        from media_platform.xhs.client import XiaoHongShuClient
        from tools import utils as mc_utils
    finally:
        os.chdir(os.getcwd())  # 恢复

    _page = await get_page_for_platform("xhs", "https://www.xiaohongshu.com/explore")

    user_agent = await _page.evaluate("() => navigator.userAgent")
    ctx = _page.context
    cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
        ctx, urls=["https://www.xiaohongshu.com"],
    )
    headers = {
        "User-Agent": user_agent,
        "Cookie": cookie_str,
        "Host": "edith.xiaohongshu.com",
        "Origin": "https://www.xiaohongshu.com",
        "Referer": "https://www.xiaohongshu.com/",
        "Content-Type": "application/json;charset=UTF-8",
    }

    _client = XiaoHongShuClient(
        timeout=30, proxy=_get_proxy(), headers=headers,
        playwright_page=_page, cookie_dict=cookie_dict,
    )
    _client_initialized = True
    logger.info("XiaoHongShuClient 已就绪 (CDP)")
    return _client


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    m = _XHS_NOTE_RE.search(url)
    return m.group(1) or m.group(2) if m else None


def normalize(item: dict) -> dict:
    note = item.get("note_card", item) or item
    note_id = note.get("note_id", "") or note.get("id", "")
    title = note.get("display_title", "") or note.get("title", "")
    desc = note.get("desc", "") or ""
    author_info = note.get("user", {}) or {}
    author_name = author_info.get("nickname", "") if isinstance(author_info, dict) else ""
    stats = note.get("interact_info", {}) or {}
    # 视频时长
    video_info = note.get("video", {}) or {}
    duration_sec = int(video_info.get("duration", 0)) if video_info else 0
    # 图片笔记无时长
    if not duration_sec and note.get("type") == "video" and video_info:
        duration_sec = int(video_info.get("video_duration", 0))
    create_time = int(note.get("time", 0))

    # 提取 xsec_token（后续获取详情需要）
    xsec_token = note.get("xsec_token", "")

    return {
        "video_id": str(note_id),
        "title": title[:200] if title else (desc[:200] if desc else ""),
        "desc": desc[:500] if desc else "",
        "publish_time": create_time,
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec),
        "video_url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else "",
        "platform": "xiaohongshu",
        "author": author_name,
        "view_count": int(stats.get("liked_count", 0)),
        # 小红书特有字段
        "xsec_token": xsec_token,
        "note_type": note.get("type", ""),
    }


def _fmt_duration(sec: int) -> str:
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 搜索 + 创作者 + 下载
# ---------------------------------------------------------------------------

async def _search_via_cdp(keyword: str, limit: int = 10) -> list[dict]:
    client = await _ensure_client()
    try:
        resp = await client.get_note_by_keyword(keyword, page=1, page_size=limit)
    except Exception as e:
        err_msg = str(e)
        if "DataFetchError" in err_msg:
            logger.warning("小红书搜索失败（API 拒接，可能未登录或风控）: %s", err_msg[:120])
        else:
            logger.warning("小红书搜索失败: %s", e)
        return []

    items = resp.get("items", []) if isinstance(resp, dict) else []
    results = [normalize(it) for it in items[:limit]]
    logger.info("🔍 小红书搜索 '%s': %d 条", keyword, len(results))
    return results


async def _get_creator_via_cdp(creator_id: str, limit: int = 10) -> list[dict]:
    client = await _ensure_client()
    try:
        resp = await client.get_all_notes_by_creator(creator_id)
    except Exception as e:
        logger.warning("小红书创作者查询失败: %s", e)
        return []
    items = resp.get("notes", resp) if isinstance(resp, dict) else resp
    results = [normalize(it) for it in items[:limit]]
    logger.info("👤 小红书创作者 %s: %d 篇笔记", creator_id, len(results))
    return results


async def _download_via_cdp(note_url: str, file_name: str) -> dict:
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        return {"status": "success", "local_path": str(target), "platform": "xiaohongshu", "cached": True}

    note_id = extract_video_id(note_url)
    if not note_id:
        return {"status": "error", "error": f"无法解析小红书笔记 ID: {note_url}", "video_url": note_url}

    # XHS download needs xsec_token from search results — 从 URL 无法单独获取
    # 回退：直接下载封面/图片
    client = await _ensure_client()
    try:
        # 尝试通过短链获取
        short_resp = await client.get_note_short_url(note_id)
        items = short_resp.get("items", []) if isinstance(short_resp, dict) else []
        if items:
            note_card = items[0].get("note_card", items[0])
            return await _download_note_media(note_card, str(target), file_name)
    except Exception as e:
        pass

    return {"status": "error", "error": "小红书笔记下载需配合搜索使用（需要 xsec_token）", "video_url": note_url}


async def _download_note_media(note: dict, target: Path, file_name: str) -> dict:
    """从笔记数据中提取并下载媒体文件。"""
    video_info = note.get("video", {}) or {}
    media_url = video_info.get("media", {}).get("stream", {}).get("h264", [{}])[0].get("master_url", "")

    if not media_url:
        # 尝试图片
        image_list = note.get("image_list", []) or []
        if image_list:
            media_url = image_list[0].get("url_default", "") or image_list[0].get("url", "")

    if not media_url:
        return {"status": "error", "error": "未找到可下载的媒体链接"}

    try:
        async with httpx.AsyncClient(proxy=_get_proxy(), timeout=120, follow_redirects=True) as http:
            resp = await http.get(media_url)
            resp.raise_for_status()
            target.write_bytes(resp.content)
    except Exception as e:
        return {"status": "error", "error": f"媒体文件下载失败: {e}"}

    return {"status": "success", "local_path": str(target), "platform": "xiaohongshu"}


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class XiaohongshuPlatform(Platform):
    name: ClassVar[str] = "xiaohongshu"
    aliases: ClassVar[tuple[str, ...]] = ("xhs", "小红书", "红书")

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return extract_video_id(url)

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout, proxy=_get_proxy())

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
        raise NotImplementedError("小红书暂不支持热榜")

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        return await _search_via_cdp(keyword, limit)

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        return await _get_creator_via_cdp(creator, limit)

    @staticmethod
    def download(video_url: str, file_name: str) -> dict:
        import asyncio as _asyncio
        return _asyncio.run(_download_via_cdp(video_url, file_name))


register(XiaohongshuPlatform)
