"""快手平台适配：MediaCrawler CDP 搜索 + 创作者 + 下载。

使用 KuaiShouClient (GraphQL API)，与抖音共享浏览器管理。
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

# 共享 MediaCrawler 依赖
_MEDIACRAWLER_ROOT = str(Path.home() / "Code" / "MediaCrawler")
_mc_venv = str(Path(_MEDIACRAWLER_ROOT) / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
if not os.path.isdir(_mc_venv):
    _venv_lib = Path(_MEDIACRAWLER_ROOT) / ".venv" / "lib"
    _candidates = sorted(_venv_lib.glob("python*/site-packages")) if _venv_lib.exists() else []
    _mc_venv = str(_candidates[0]) if _candidates else ""
if _mc_venv not in sys.path:
    sys.path.insert(0, _mc_venv)
if _MEDIACRAWLER_ROOT not in sys.path:
    sys.path.insert(0, _MEDIACRAWLER_ROOT)
_original_cwd = os.getcwd()

_WORKSPACE = Path(settings.workspace_dir).resolve()
_USER_DATA_DIR = _WORKSPACE / ".kuaishou_browser"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.kuaishou.com/",
}

_KS_VIDEO_ID_RE = re.compile(r"/short-video/(\w+)")
_KS_PROFILE_RE = re.compile(r"/profile/(\w+)")


def _get_proxy() -> str | None:
    return settings.youtube_proxy or None


# ---------------------------------------------------------------------------
# 浏览器管理（模块级单例，与 douyin.py 同模式）
# ---------------------------------------------------------------------------

_playwright = None
_browser_context: BrowserContext | None = None
_page: Page | None = None
_client = None
_client_initialized = False


async def _ensure_client():
    global _playwright, _browser_context, _page, _client, _client_initialized

    if _client_initialized and _client is not None:
        return _client

    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        import config as mc_config
        mc_config.PLATFORM = "ks"
        mc_config.ENABLE_CDP_MODE = False
        mc_config.HEADLESS = False
        mc_config.ENABLE_GET_MEIDAS = False
        mc_config.ENABLE_GET_COMMENTS = False

        from media_platform.kuaishou.client import KuaiShouClient
        from tools import utils as mc_utils
    finally:
        os.chdir(_original_cwd)

    _playwright = await async_playwright().start()
    _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _browser_context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(_USER_DATA_DIR),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _page = await _browser_context.new_page()
    await _page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    login_needed = True
    try:
        await _page.goto("https://www.kuaishou.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        cookies = await _browser_context.cookies()
        for c in cookies:
            if c.get("name") == "kuaishou.server.web_st" and c.get("value"):
                login_needed = False
                logger.info("快手登录态有效，跳过登录")
                break
    except Exception:
        pass

    if login_needed:
        print("\n" + "=" * 60)
        print("  快手未登录 — 请在弹出的浏览器窗口中扫码登录")
        print("  (等待最多 120 秒...)")
        print("=" * 60 + "\n")
        try:
            await _page.goto("https://www.kuaishou.com/", wait_until="domcontentloaded", timeout=15000)
            for i in range(120):
                await asyncio.sleep(1)
                try:
                    cookies = await _browser_context.cookies()
                    if any(c.get("name") == "kuaishou.server.web_st" and c.get("value") for c in cookies):
                        logger.info("快手登录成功！")
                        break
                except Exception:
                    pass
            else:
                logger.warning("登录超时（120s），将在未登录状态下继续")
        except Exception as e:
            logger.warning("快手登录异常: %s", e)

    cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
        _browser_context, urls=["https://www.kuaishou.com"],
    )
    headers = dict(DEFAULT_HEADERS)
    headers["Cookie"] = cookie_str

    _client = KuaiShouClient(
        timeout=30, proxy=_get_proxy(), headers=headers,
        playwright_page=_page, cookie_dict=cookie_dict,
    )
    _client_initialized = True
    logger.info("KuaiShouClient 已就绪")
    return _client


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    m = _KS_VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def normalize(item: dict) -> dict:
    photo = item.get("photo", item) or item
    video_id = photo.get("id", "")
    caption = photo.get("caption", "") or ""
    author_info = item.get("author", {}) or {}
    author_name = author_info.get("name", "") if isinstance(author_info, dict) else ""
    duration_sec = int(photo.get("duration", 0))
    create_time = int(photo.get("timestamp", 0))
    return {
        "video_id": str(video_id),
        "title": caption[:200] if caption else "",
        "desc": caption[:500] if caption else "",
        "publish_time": create_time,
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec),
        "video_url": f"https://www.kuaishou.com/short-video/{video_id}" if video_id else "",
        "platform": "kuaishou",
        "author": author_name,
        "view_count": int(photo.get("viewCount", 0)),
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
        resp = await client.search_info_by_keyword(keyword, pcursor="")
    except Exception as e:
        logger.warning("快手搜索失败: %s", e)
        return []

    videos = resp.get("visionSearchPhoto", {}).get("feeds", []) if isinstance(resp, dict) else []
    results = [normalize(v) for v in videos[:limit]]
    logger.info("🔍 快手搜索 '%s': %d 条", keyword, len(results))
    return results


async def _get_creator_via_cdp(creator_id: str, limit: int = 10) -> list[dict]:
    client = await _ensure_client()
    try:
        resp = await client.get_all_videos_by_creator(creator_id)
    except Exception as e:
        logger.warning("快手创作者查询失败: %s", e)
        return []
    results = [normalize(v) for v in resp[:limit]]
    logger.info("👤 快手创作者 %s: %d 个视频", creator_id, len(results))
    return results


async def _download_via_cdp(video_url: str, file_name: str) -> dict:
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        return {"status": "success", "local_path": str(target), "platform": "kuaishou", "cached": True}

    photo_id = extract_video_id(video_url)
    if not photo_id:
        return {"status": "error", "error": f"无法解析快手视频 ID: {video_url}", "video_url": video_url}

    client = await _ensure_client()
    try:
        detail = await client.get_video_info(photo_id)
    except Exception as e:
        return {"status": "error", "error": f"获取视频详情失败: {e}", "video_url": video_url}

    photo = (detail.get("visionVideoDetail", {}) or detail).get("photo", {}) or {}
    play_url = photo.get("photoUrl", "")
    if not play_url:
        return {"status": "error", "error": "未找到可下载的视频链接", "video_url": video_url}

    try:
        async with httpx.AsyncClient(proxy=_get_proxy(), timeout=120, follow_redirects=True) as http:
            resp = await http.get(play_url)
            resp.raise_for_status()
            target.write_bytes(resp.content)
    except Exception as e:
        return {"status": "error", "error": f"视频文件下载失败: {e}", "video_url": video_url}

    return {"status": "success", "local_path": str(target), "platform": "kuaishou"}


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class KuaishouPlatform(Platform):
    name: ClassVar[str] = "kuaishou"
    aliases: ClassVar[tuple[str, ...]] = ("ks", "快手")

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
        raise NotImplementedError("快手暂不支持热榜（平台无公开接口）")

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


register(KuaishouPlatform)
