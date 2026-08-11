"""抖音平台适配：公开热榜 API + MediaCrawler CDP 搜索/创作者/下载。

P0: 公开热榜 API — 免费、免登录
P2: MediaCrawler CDP — Playwright 浏览器 → 搜索 + 创作者 + 下载
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

# ---------------------------------------------------------------------------
# 导入 MediaCrawler（本地仓库）
# ---------------------------------------------------------------------------

_MEDIACRAWLER_ROOT = str(Path.home() / "Code" / "MediaCrawler")
_original_cwd = os.getcwd()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_HOT_SEARCH_API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"
_WORKSPACE = Path(settings.workspace_dir).resolve()
_USER_DATA_DIR = _WORKSPACE / ".douyin_browser"  # Playwright 持久化目录

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}

_DY_VIDEO_ID_RE = re.compile(r"/video/(\d{15,20})")
_DY_SHORT_RE = re.compile(r"v\.douyin\.com/(\w+)")


def _get_proxy() -> str | None:
    p = settings.youtube_proxy
    return p if p else None


# ---------------------------------------------------------------------------
# Playwright 浏览器管理（模块级单例）
# ---------------------------------------------------------------------------

_playwright = None
_browser_context: BrowserContext | None = None
_page: Page | None = None
_client = None  # DouYinClient
_client_initialized = False


async def _ensure_client():
    """初始化或恢复 MediaCrawler DouYinClient（含 Playwright 浏览器）。"""
    global _playwright, _browser_context, _page, _client, _client_initialized

    if _client_initialized and _client is not None:
        return _client

    # 切换 cwd 到 MediaCrawler 根（execjs 需要相对路径 libs/douyin.js）
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        import config as mc_config  # noqa: F811
        mc_config.PLATFORM = "dy"
        mc_config.ENABLE_CDP_MODE = True
        mc_config.CDP_CONNECT_EXISTING = True
        mc_config.LOGIN_TYPE = "qrcode"
        mc_config.SAVE_LOGIN_STATE = True
        mc_config.HEADLESS = False
        mc_config.CDP_HEADLESS = False
        mc_config.ENABLE_GET_MEIDAS = False
        mc_config.ENABLE_GET_COMMENTS = False
        mc_config.CRAWLER_MAX_NOTES_COUNT = 20

        from media_platform.douyin.client import DouYinClient
        from media_platform.douyin.login import DouYinLogin
    finally:
        os.chdir(_original_cwd)

    _playwright = await async_playwright().start()

    # 直接启动持久化浏览器（WSL 无法 CDP 连接 Windows Chrome）
    _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _browser_context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(_USER_DATA_DIR),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _page = await _browser_context.new_page()
    await _page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    cdp_connected = False

    # 检查登录状态（CDP 连接通常意味着已登录）
    login_needed = not cdp_connected
    try:
        await _page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)
        if _page.is_closed():
            pages = _browser_context.pages
            _page = pages[0] if pages else await _browser_context.new_page()
        local_storage = await _page.evaluate("() => window.localStorage")
        if local_storage.get("HasUserLogin", "") == "1":
            login_needed = False
            logger.info("抖音登录态有效，跳过登录")
    except Exception:
        pass

    if login_needed:
        print("\n" + "=" * 60)
        print("  抖音未登录 — 请在弹出的浏览器窗口中扫码登录")
        print("  (等待最多 120 秒...)")
        print("=" * 60 + "\n")
        try:
            os.chdir(_MEDIACRAWLER_ROOT)
            try:
                from media_platform.douyin.login import DouYinLogin
                # 直接检查登录（QR 码在浏览器窗口中可见，无需额外 display）
                await _page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=15000)

                # 简化登录：点击登录按钮 → 等待用户扫码
                try:
                    login_button = _page.locator("xpath=//p[text() = '登录']")
                    await login_button.click(timeout=5000)
                    await asyncio.sleep(1)
                except Exception:
                    pass  # 登录弹窗可能已自动出现

                # 轮询等待用户扫码（最多 120s）
                for i in range(120):
                    await asyncio.sleep(1)
                    try:
                        ls = await _page.evaluate("() => window.localStorage")
                        if ls.get("HasUserLogin", "") == "1":
                            logger.info("抖音登录成功！")
                            break
                    except Exception:
                        pass
                else:
                    logger.warning("登录超时（120s），将在未登录状态下继续")
            finally:
                os.chdir(_original_cwd)
        except Exception as e:
            logger.warning("抖音登录异常: %s", e)

    # 提取 cookie 构建 DouYinClient
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        from tools import utils as mc_utils
    finally:
        os.chdir(_original_cwd)
    cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
        _browser_context,
        urls=["https://douyin.com", "https://www.douyin.com"],
    )
    headers = dict(DEFAULT_HEADERS)
    headers["Cookie"] = cookie_str

    _client = DouYinClient(
        timeout=30,
        proxy=_get_proxy(),
        headers=headers,
        playwright_page=_page,
        cookie_dict=cookie_dict,
    )
    _client_initialized = True
    logger.info("DouYinClient 已就绪")
    return _client


async def _close_client():
    """关闭 Playwright 浏览器。"""
    global _browser_context, _page, _client, _client_initialized
    if _browser_context:
        try:
            await _browser_context.close()
        except Exception:
            pass
        _browser_context = None
    _page = None
    _client = None
    _client_initialized = False


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    """从抖音 URL 提取视频 ID。"""
    if url.isdigit() and len(url) >= 15:
        return url
    m = _DY_VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    m2 = _DY_SHORT_RE.search(url)
    if m2:
        return f"dy_short_{m2.group(1)}"
    return None


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=timeout, proxy=_get_proxy(),
    )


def normalize(item: dict) -> dict:
    if "word" in item and "hot_value" in item:
        return _normalize_trending(item)
    return _normalize_video(item)


def _normalize_trending(item: dict) -> dict:
    word = item.get("word", "")
    video_count = item.get("video_count", 0)
    hot_value = item.get("hot_value", 0)
    event_time = item.get("event_time", 0)
    group_id = item.get("group_id", "")
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
        "duration": 0,
        "duration_text": "",
        "video_url": "",  # 热搜话题不是具体视频，需用 search_keyword 搜索后获取真实视频
        "platform": "douyin",
        "author": "",
        "view_count": video_count,
        "hot_value": hot_value,
        "cover_url": cover_url,
        "is_trending_topic": True,
        "search_keyword": word,  # Agent 应用此关键词搜索获取真实视频列表
    }


def _normalize_video(item: dict) -> dict:
    """从 MediaCrawler/Douyin API 响应归一化视频数据。

    支持格式：
    - Douyin API aweme_detail 返回 (aweme_detail 对象)
    - 搜索结果的 aweme_info 嵌套格式
    """
    # 兼容嵌套格式：搜索结果是 {aweme_info: {...}}
    aweme = item.get("aweme_info", item) or item

    aweme_id = str(aweme.get("aweme_id", ""))
    desc = aweme.get("desc", "") or ""
    author_info = aweme.get("author", {}) or {}
    author_name = author_info.get("nickname", "") if isinstance(author_info, dict) else ""
    stats = aweme.get("statistics", {}) or {}
    # duration 在 video.duration 字段中（毫秒）
    video_info = aweme.get("video", {}) or {}
    duration_ms = aweme.get("duration") or video_info.get("duration", 0)
    duration_sec = int(duration_ms / 1000) if duration_ms and duration_ms > 1000 else (int(duration_ms) if duration_ms else 0)
    create_time = aweme.get("create_time", 0)

    return {
        "video_id": aweme_id,
        "title": desc[:200] if desc else "",
        "desc": desc[:500] if desc else "",
        "publish_time": int(create_time),
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec),
        "video_url": f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
        "platform": "douyin",
        "author": author_name,
        "view_count": int(stats.get("digg_count", 0)),
    }


def _fmt_duration(sec: int) -> str:
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 热榜（P0）
# ---------------------------------------------------------------------------

async def fetch_hot_search(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
    try:
        resp = await client.get(_HOT_SEARCH_API)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("抖音热榜 API 失败: %s", e)
        return []
    trending = data.get("data", {}).get("trending_list", [])
    logger.info("🔥 抖音热榜: %d 条 (更新时间: %s)", len(trending),
                data.get("data", {}).get("active_time", ""))
    return [normalize(it) for it in trending[:limit]]


# ---------------------------------------------------------------------------
# 搜索 + 创作者（P2 — MediaCrawler CDP）
# ---------------------------------------------------------------------------

async def _search_via_cdp(keyword: str, limit: int = 10) -> list[dict]:
    """通过 MediaCrawler DouYinClient 搜索视频。"""
    client = await _ensure_client()
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        from media_platform.douyin.field import SearchChannelType
    finally:
        os.chdir(_original_cwd)
    try:
        resp = await client.search_info_by_keyword(
            keyword=keyword,
            offset=0,
            search_channel=SearchChannelType.GENERAL,
        )
    except Exception as e:
        logger.warning("抖音搜索失败: %s", e)
        return []

    # 解析搜索结果
    data = resp.get("data", []) if isinstance(resp, dict) else []
    if not data:
        return []

    results = []
    for item in data:
        aweme_info = item.get("aweme_info") or item.get("aweme_detail") or item
        if aweme_info:
            results.append(normalize(aweme_info))
    logger.info("🔍 抖音搜索 '%s': %d 条", keyword, len(results))
    return results[:limit]


async def _get_creator_via_cdp(creator_id: str, limit: int = 10) -> list[dict]:
    """通过 MediaCrawler DouYinClient 获取创作者视频。"""
    client = await _ensure_client()
    try:
        os.chdir(_MEDIACRAWLER_ROOT)
        try:
            from media_platform.douyin.help import parse_creator_info_from_url
            info = parse_creator_info_from_url(creator_id)
            sec_user_id = info.sec_user_id
        finally:
            os.chdir(_original_cwd)
    except (ValueError, ImportError):
        sec_user_id = creator_id

    try:
        aweme_list = await client.get_all_user_aweme_posts(sec_user_id)
    except Exception as e:
        logger.warning("抖音创作者查询失败: %s", e)
        return []

    results = [normalize(item) for item in aweme_list[:limit]]
    logger.info("👤 抖音创作者 %s: %d 个视频", sec_user_id, len(results))
    return results


# ---------------------------------------------------------------------------
# 下载（P2 — MediaCrawler → httpx 下载视频文件）
# ---------------------------------------------------------------------------

async def _download_via_cdp(video_url: str, file_name: str) -> dict:
    """通过 MediaCrawler 获取视频下载链接，httpx 下载文件。"""
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        logger.info("下载命中缓存: %s", target)
        return {"status": "success", "local_path": str(target), "platform": "douyin", "cached": True}

    logger.info("📥 抖音下载开始: url=%s", video_url)

    # 1. 提取 aweme_id
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        from media_platform.douyin.help import parse_video_info_from_url
        video_info = parse_video_info_from_url(video_url)
        aweme_id = video_info.aweme_id
        logger.info("  MediaCrawler 解析 → aweme_id=%s", aweme_id)
    except Exception as e:
        logger.warning("MediaCrawler 解析 URL 失败，回退内置正则: %s", e)
        aweme_id = extract_video_id(video_url)
        logger.info("  内置正则解析 → aweme_id=%s", aweme_id)
    finally:
        os.chdir(_original_cwd)

    # 统一校验
    if not aweme_id or (isinstance(aweme_id, str) and aweme_id.startswith("dy_short_")):
        logger.error("  无法解析视频 ID: url=%s", video_url)
        return {"status": "error", "error": f"无法解析视频 ID: {video_url}", "video_url": video_url}
    aweme_id = str(aweme_id)  # 确保是字符串

    # 2. 获取视频详情（含下载链接）
    logger.info("  获取视频详情: aweme_id=%s", aweme_id)
    client = await _ensure_client()
    try:
        detail = await client.get_video_by_id(aweme_id)
        logger.info("  get_video_by_id → type=%s, has_video=%s",
                     type(detail).__name__ if detail else "None",
                     bool(detail.get("video")) if isinstance(detail, dict) else "N/A")
    except Exception as e:
        logger.error("  获取视频详情失败: %s", e)
        return {"status": "error", "error": f"获取视频详情失败: {e}", "video_url": video_url}

    if not detail:
        logger.error("  视频不存在或已被删除: aweme_id=%s", aweme_id)
        return {"status": "error", "error": "视频不存在或已被删除", "video_url": video_url}

    # 3. 提取无水印下载链接
    video_data = detail.get("video", {}) if isinstance(detail, dict) else {}
    play_addr = video_data.get("play_addr", {}) or {}
    download_addr = video_data.get("download_addr", {}) or {}
    media_url = (
        download_addr.get("url_list", [""])[0]
        or play_addr.get("url_list", [""])[0]
    )
    logger.info("  下载链接: %s...", media_url[:80] if media_url else "EMPTY")

    if not media_url:
        # 打印 detail 结构帮助调试
        if isinstance(detail, dict):
            logger.error("  detail keys: %s", list(detail.keys())[:10])
            if video_data:
                logger.error("  video keys: %s", list(video_data.keys())[:10])
        return {"status": "error", "error": "未找到可下载的视频链接", "video_url": video_url}

    # 4. 下载视频文件（需要 Referer + UA 模拟正常请求）
    logger.info("  httpx 下载中…")
    try:
        dl_headers = {
            **DEFAULT_HEADERS,
            "Referer": "https://www.douyin.com/video/" + aweme_id,
        }
        async with httpx.AsyncClient(
            headers=dl_headers, proxy=_get_proxy(),
            timeout=120, follow_redirects=True,
        ) as http:
            resp = await http.get(media_url)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            logger.info("  ✅ 下载完成: %d KB → %s", len(resp.content) // 1024, target)
    except Exception as e:
        logger.error("  视频文件下载失败: %s", e)
        return {"status": "error", "error": f"视频文件下载失败: {e}", "video_url": video_url}

    return {"status": "success", "local_path": str(target), "platform": "douyin"}


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
        return await fetch_hot_search(client, limit)

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索（MediaCrawler CDP）。"""
        return await _search_via_cdp(keyword, limit)

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频（MediaCrawler CDP）。"""
        return await _get_creator_via_cdp(creator, limit)

    @staticmethod
    def download(video_url: str, file_name: str) -> dict:
        """下载抖音无水印视频（MediaCrawler CDP → httpx）。"""
        import asyncio as _asyncio
        return _asyncio.run(_download_via_cdp(video_url, file_name))


# 注册到全局注册表
register(DouyinPlatform)
