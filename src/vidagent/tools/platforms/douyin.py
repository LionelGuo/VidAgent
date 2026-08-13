"""抖音平台适配：公开热榜 API + MediaCrawler CDP 搜索/创作者/下载。

P0: 公开热榜 API — 免费、免登录
P2: MediaCrawler CDP — 连接 Windows Chrome 远程调试端口（:9222），
    复用浏览器登录态完成搜索 / 创作者 / 下载（签名由 MediaCrawler 内部处理）。

设计要点（见 docs/adr/0004）：
- 只在导入期 chdir 到 MediaCrawler 根（help.py 模块级编译 libs/douyin.js），
  运行时零 cwd 依赖，避免多线程下 chdir 竞态。
- asyncio.Lock 串行化 DouYinClient 调用（MediaCrawler 按单爬虫设计，
  每次请求前 page.evaluate 读 localStorage 取 msToken）。
- page 失效（is_closed / evaluate 超时）→ invalidate_page + 重建 client。
- 绝不关闭 CDP 浏览器 context——那是用户的真实浏览器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, ClassVar

import httpx

from vidagent.config import settings
from vidagent.tools.platforms import Platform, register
from ._cdp_browser import (
    get_page_for_platform,
    get_mc_utils,
    invalidate_page,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_HOT_SEARCH_API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
    "Origin": "https://www.douyin.com",
}

_DY_VIDEO_ID_RE = re.compile(r"/video/(\d{15,20})")
_DY_SHORT_RE = re.compile(r"v\.douyin\.com/(\w+)")
_SEARCH_URL_RE = re.compile(r"douyin\.com/search/(.+)")

_CLIENT_TIMEOUT = 60        # DouYinClient 单请求 HTTP 超时（秒）
_CALL_TIMEOUT = 45          # 单次 client 调用上限（防 page.evaluate 挂起）
_LOGIN_POLL_SECONDS = 120   # 扫码登录轮询上限
_XMST_POLL_SECONDS = 10     # 页面加载后等 xmst（msToken）就绪上限

CDP_GUIDE_MSG = (
    "无法连接 Windows Chrome 调试端口 9222。请确认："
    "1) Chrome 已运行并开启远程调试（chrome://inspect/#remote-debugging 勾选，"
    "或命令行 --remote-debugging-port=9222 启动）；"
    "2) 浏览器弹出的调试连接确认框已点「接受」。"
)

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DouyinClientError(RuntimeError):
    """抖音客户端不可用（CDP 连接失败 / 页面异常）。"""


class DouyinLoginError(DouyinClientError):
    """抖音未登录且扫码引导失败。"""


# ---------------------------------------------------------------------------
# MediaCrawler 模块一次性导入（导入期 chdir，运行时零 cwd 依赖）
# ---------------------------------------------------------------------------

_DouYinClientCls: Any = None
_SearchChannelType: Any = None
_ParseVideoInfo: Any = None
_ParseCreatorInfo: Any = None


def _import_mediacrawler() -> None:
    """导入 MediaCrawler 抖音模块（首次调用时执行）。

    MediaCrawler 唯一的导入期 cwd 依赖是 douyin/help.py 模块级编译
    libs/douyin.js，因此只在导入时 chdir 一次，之后永久切回。
    """
    global _DouYinClientCls, _SearchChannelType, _ParseVideoInfo, _ParseCreatorInfo
    if _DouYinClientCls is not None:
        return

    from ._cdp_browser import chdir_mc
    mc_root = chdir_mc()
    prev_cwd = os.getcwd()
    os.chdir(mc_root)
    try:
        import config as mc_config
        # VidAgent 专属配置：CDP 连接现有浏览器（不启动新进程、不保存登录态）
        mc_config.PLATFORM = "dy"
        mc_config.ENABLE_CDP_MODE = True
        mc_config.CDP_CONNECT_EXISTING = True
        mc_config.CDP_HEADLESS = False
        mc_config.AUTO_CLOSE_BROWSER = False
        mc_config.SAVE_LOGIN_STATE = False
        mc_config.LOGIN_TYPE = "qrcode"
        mc_config.BROWSER_LAUNCH_TIMEOUT = 15
        mc_config.ENABLE_GET_MEIDAS = False
        mc_config.ENABLE_GET_COMMENTS = False
        mc_config.CRAWLER_MAX_NOTES_COUNT = 20

        from media_platform.douyin.client import DouYinClient
        from media_platform.douyin.field import SearchChannelType
        from media_platform.douyin.help import (
            parse_video_info_from_url,
            parse_creator_info_from_url,
        )
        _DouYinClientCls = DouYinClient
        _SearchChannelType = SearchChannelType
        _ParseVideoInfo = parse_video_info_from_url
        _ParseCreatorInfo = parse_creator_info_from_url
    finally:
        os.chdir(prev_cwd)

    logger.info("MediaCrawler 抖音模块已导入（签名 JS 已编译）")


def _get_proxy() -> str | None:
    return settings.youtube_proxy or None


# ---------------------------------------------------------------------------
# 客户端管理（CDP 连接 Windows Chrome，复用登录态）
# ---------------------------------------------------------------------------

_client: Any = None
_client_page: Any = None
# 串行化 DouYinClient 调用：MediaCrawler 按单爬虫设计（共享单 page，
# 每次请求 evaluate 读 localStorage），并发调用同一 client 有竞态风险。
_client_lock = asyncio.Lock()


async def _ensure_client() -> Any:
    """初始化/恢复 DouYinClient（CDP 连接 Windows Chrome）。"""
    global _client, _client_page

    _import_mediacrawler()

    # page 失效检测：client 持有的 page 已关闭 → 重建
    if _client is not None and _client_page is not None:
        try:
            if not _client_page.is_closed():
                return _client
        except Exception:
            pass
        _client = None
        _client_page = None

    try:
        page = await get_page_for_platform("dy", "https://www.douyin.com")
    except RuntimeError:
        raise DouyinClientError(CDP_GUIDE_MSG) from None
    except Exception as e:
        await invalidate_page("dy")
        raise DouyinClientError(f"抖音页面加载失败: {e}") from e

    await _wait_xmst(page)

    mc_utils = get_mc_utils()
    cookie_str, cookie_dict = await mc_utils.convert_browser_context_cookies(
        page.context, urls=["https://douyin.com", "https://www.douyin.com"],
    )
    ua = await page.evaluate("() => navigator.userAgent")

    headers = dict(DEFAULT_HEADERS)
    headers["User-Agent"] = ua
    headers["Cookie"] = cookie_str

    _client = _DouYinClientCls(
        timeout=_CLIENT_TIMEOUT,
        proxy=_get_proxy(),
        headers=headers,
        playwright_page=page,
        cookie_dict=cookie_dict,
    )
    _client_page = page

    # 登录态检查：未登录 → 弹登录框引导扫码
    if not await _is_logged_in(page, cookie_dict):
        await _guide_qr_login(page)
        if await _is_logged_in(page, cookie_dict):
            # 登录后刷新 cookie 到 client
            await _client.update_cookies(
                page.context,
                urls=["https://douyin.com", "https://www.douyin.com"],
            )
        else:
            _client = None
            _client_page = None
            raise DouyinLoginError(
                f"抖音未登录：扫码登录超时（{_LOGIN_POLL_SECONDS}s），"
                "请先在 Chrome 中登录抖音后重试"
            )

    logger.info("DouYinClient 已就绪 (CDP)")
    return _client


async def _wait_xmst(page: Any) -> None:
    """等页面 localStorage 种出 xmst（msToken，API 请求必需）。"""
    for _ in range(_XMST_POLL_SECONDS * 2):
        try:
            xmst = await asyncio.wait_for(
                page.evaluate("() => window.localStorage.getItem('xmst')"),
                timeout=3,
            )
            if xmst:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    logger.warning("xmst 未在 %ds 内就绪，继续尝试（可能被风控）", _XMST_POLL_SECONDS)


async def _is_logged_in(page: Any, cookie_dict: dict) -> bool:
    """登录态判定：localStorage HasUserLogin 或 cookie LOGIN_STATUS。"""
    try:
        ls = await asyncio.wait_for(
            page.evaluate("() => window.localStorage"), timeout=5,
        )
        if ls.get("HasUserLogin") == "1":
            return True
    except Exception:
        pass
    return any(
        str(k).upper() == "LOGIN_STATUS" and str(v) == "1"
        for k, v in (cookie_dict or {}).items()
    )


async def _guide_qr_login(page: Any) -> None:
    """在 CDP 页面上打开登录弹窗，等待用户扫码（最多 120s）。"""
    logger.info("抖音未登录 — 在浏览器页面引导扫码登录（最多 %ds）…", _LOGIN_POLL_SECONDS)
    try:
        try:
            await page.goto(
                "https://www.douyin.com/",
                wait_until="domcontentloaded", timeout=15000,
            )
        except Exception:
            pass
        try:
            login_btn = page.locator("xpath=//p[text() = '登录']")
            await login_btn.click(timeout=5000)
        except Exception:
            pass  # 登录弹窗可能已自动出现
    except Exception as e:
        logger.warning("登录弹窗触发失败（不影响轮询）: %s", e)

    for _ in range(_LOGIN_POLL_SECONDS):
        await asyncio.sleep(1)
        try:
            ls = await asyncio.wait_for(
                page.evaluate("() => window.localStorage"), timeout=2,
            )
            if ls.get("HasUserLogin") == "1":
                logger.info("抖音扫码登录成功")
                return
        except Exception:
            pass


async def _client_call(coro_factory: Callable[[Any], Any]) -> Any:
    """串行化执行 client 调用 + 超时防护；挂起时失效重建 page/client。"""
    global _client, _client_page
    async with _client_lock:
        client = await _ensure_client()
        try:
            return await asyncio.wait_for(
                coro_factory(client), timeout=_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "抖音 client 调用超时（%ds），判定 page 挂起，重建", _CALL_TIMEOUT,
            )
            await invalidate_page("dy")
            _client = None
            _client_page = None
            raise DouyinClientError(
                "抖音请求超时（浏览器页面异常），请重试"
            ) from None


async def _close_client() -> None:
    """重置客户端引用（不关闭 CDP 浏览器——那是用户的真实浏览器）。"""
    global _client, _client_page
    await invalidate_page("dy")
    _client = None
    _client_page = None


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
        "video_url": f"https://www.douyin.com/search/{word}" if word else "",
        "platform": "douyin",
        "author": "",
        "view_count": video_count,
        "hot_value": hot_value,
        "cover_url": cover_url,
    }


def _normalize_video(item: dict) -> dict:
    """从 MediaCrawler/Douyin API 响应归一化视频数据。

    支持格式：
    - Douyin API aweme_detail 返回 (aweme_detail 对象)
    - 搜索结果的 aweme_info 嵌套格式
    """
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
# 热榜（视频榜 — CDP 页面内 XHR）
# ---------------------------------------------------------------------------

async def fetch_hot_search(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
    """[兼容旧签名] 热搜词榜（话题热度，非视频）。已由视频榜方案取代。"""
    try:
        resp = await client.get(_HOT_SEARCH_API)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("抖音热搜词 API 失败: %s", e)
        return []
    trending = data.get("data", {}).get("trending_list", [])
    logger.info("🔥 抖音热搜词: %d 条 (更新时间: %s)", len(trending),
                data.get("data", {}).get("active_time", ""))
    return [normalize(it) for it in trending[:limit]]


_HOT_XHR_JS = """(count) => new Promise((resolve) => {
  const url = 'https://www.douyin.com/aweme/v1/web/channel/hotspot?device_platform=webapp&aid=6383&channel=channel_pc_web&tag_id=&count=' + count + '&Seo-Flag=0&channel_id=99&pc_client_type=1&pc_libra_divert=Windows&support_h265=1&support_dash=1&cookie_enabled=true&platform=PC';
  const xhr = new XMLHttpRequest();
  xhr.open('GET', url, true);
  xhr.withCredentials = true;
  xhr.onload = () => resolve({status: xhr.status, body: xhr.responseText});
  xhr.onerror = () => resolve({status: 0, body: ''});
  xhr.send();
})"""


async def _fetch_hot_videos_via_page(limit: int = 10) -> list[dict]:
    """通过 CDP 页面内 XHR 请求视频榜（douyin.com/hot 的「视频」tab）。

    直接 httpx / DouYinClient 签名请求会被风控拒绝（account blocked 空响应），
    而页面自身 XHR 会被 webmssdk 的 XHR hook 自动补签名，与真实浏览行为一致。
    """
    from ._cdp_browser import get_page_for_platform

    page = await get_page_for_platform("dy", "https://www.douyin.com/hot")

    # 等 webmssdk 就绪（XHR hook 生效前发出的请求无签名会被拒）
    for _ in range(24):
        ready = await page.evaluate("() => !!window.byted_acrawler")
        if ready:
            break
        await asyncio.sleep(0.5)

    for attempt in range(3):
        try:
            raw = await page.evaluate(_HOT_XHR_JS, limit)
        except Exception as e:
            logger.warning("抖音视频榜 XHR 异常(第%d次): %s", attempt + 1, e)
            raw = {"status": 0, "body": ""}
        if raw.get("status") == 200 and raw.get("body"):
            break
        await asyncio.sleep(1)

    if raw.get("status") != 200 or not raw.get("body"):
        logger.warning("抖音视频榜 XHR 失败: status=%s", raw.get("status"))
        return []
    try:
        data = json.loads(raw["body"])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("抖音视频榜响应解析失败: %s", e)
        return []
    aweme_list = data.get("aweme_list", []) or []
    if not aweme_list:
        # 空结果诊断：转储响应键（静默风控的典型信号）
        logger.warning("抖音视频榜空结果: resp_keys=%s",
                       list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        return []
    results = [_normalize_video(a) for a in aweme_list[:limit]]
    logger.info("🔥 抖音热榜: %d 个视频", len(results))
    return results


# ---------------------------------------------------------------------------
# 搜索 + 创作者（P2 — MediaCrawler CDP）
# ---------------------------------------------------------------------------

async def _search_via_cdp(keyword: str, limit: int = 10) -> list[dict]:
    """通过 MediaCrawler DouYinClient 搜索视频。"""
    try:
        resp = await _client_call(
            lambda c: c.search_info_by_keyword(
                keyword=keyword, offset=0,
                search_channel=_SearchChannelType.GENERAL,
            )
        )
    except DouyinClientError as e:
        logger.warning("抖音搜索失败: %s", e)
        return []

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
    try:
        info = _ParseCreatorInfo(creator_id)
        sec_user_id = info.sec_user_id
    except (ValueError, AttributeError):
        sec_user_id = creator_id

    try:
        aweme_list = await _client_call(
            lambda c: c.get_all_user_aweme_posts(sec_user_id)
        )
    except DouyinClientError as e:
        logger.warning("抖音创作者查询失败: %s", e)
        return []

    results = [normalize(item) for item in aweme_list[:limit]]
    logger.info("👤 抖音创作者 %s: %d 个视频", sec_user_id, len(results))
    return results


# ---------------------------------------------------------------------------
# 下载（P2 — MediaCrawler 详情 → httpx 下载视频文件）
# ---------------------------------------------------------------------------

async def _download_via_cdp(video_url: str, file_name: str,
                            progress_callback: Callable[[int], None] | None = None) -> dict:
    """通过 MediaCrawler 获取视频下载链接，httpx 下载文件。"""
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        logger.info("下载命中缓存: %s", target)
        if progress_callback:
            progress_callback(100)
        return {"status": "success", "local_path": str(target), "platform": "douyin", "cached": True}

    logger.info("📥 抖音下载开始: url=%s", video_url)

    # 0. 搜索页 URL → 解析为真实视频
    search_match = _SEARCH_URL_RE.search(video_url)
    if search_match:
        from urllib.parse import unquote
        keyword = unquote(search_match.group(1))
        logger.info("  🔍 搜索页 URL，解析关键词: '%s' → 搜索真实视频…", keyword)
        try:
            resp = await _client_call(
                lambda c: c.search_info_by_keyword(
                    keyword=keyword, offset=0,
                    search_channel=_SearchChannelType.GENERAL,
                )
            )
            data = resp.get("data", []) if isinstance(resp, dict) else []
            if data:
                for item in data:
                    aweme = item.get("aweme_info") or item.get("aweme_detail") or item
                    if aweme and aweme.get("aweme_id"):
                        video_url = f"https://www.douyin.com/video/{aweme['aweme_id']}"
                        logger.info("  ✅ 解析为: %s", video_url)
                        break
                else:
                    logger.error("  ❌ 搜索结果中无有效视频")
                    return {"status": "error", "error": f"关键词 '{keyword}' 搜索结果无有效视频", "video_url": video_url}
            else:
                logger.error("  ❌ 搜索 '%s' 无结果", keyword)
                return {"status": "error", "error": f"关键词 '{keyword}' 搜索失败：无结果", "video_url": video_url}
        except DouyinClientError as e:
            return {"status": "error", "error": str(e), "video_url": video_url}

    # 1. 提取 aweme_id
    try:
        video_info = _ParseVideoInfo(video_url)
        aweme_id = video_info.aweme_id
        logger.info("  MediaCrawler 解析 → aweme_id=%s", aweme_id)
    except Exception:
        logger.warning("MediaCrawler 解析 URL 失败，回退内置正则")
        aweme_id = extract_video_id(video_url)

    # 短链 → 解析真实 URL（MediaCrawler resolve_short_url）
    if aweme_id and str(aweme_id).startswith("dy_short_"):
        logger.info("  检测到短链，解析真实地址…")
        try:
            resolved = await _client_call(lambda c: c.resolve_short_url(video_url))
            if resolved:
                video_url = resolved
                aweme_id = extract_video_id(video_url)
                logger.info("  ✅ 短链解析为: %s", video_url)
        except DouyinClientError as e:
            return {"status": "error", "error": str(e), "video_url": video_url}

    # 统一校验
    if not aweme_id or (isinstance(aweme_id, str) and aweme_id.startswith("dy_short_")):
        logger.error("  无法解析视频 ID: url=%s", video_url)
        return {"status": "error", "error": f"无法解析视频 ID: {video_url}", "video_url": video_url}
    aweme_id = str(aweme_id)

    # 2. 获取视频详情（含下载链接）
    logger.info("  获取视频详情: aweme_id=%s", aweme_id)
    try:
        detail = await _client_call(lambda c: c.get_video_by_id(aweme_id))
    except DouyinClientError as e:
        return {"status": "error", "error": str(e), "video_url": video_url}

    if not detail:
        logger.error("  视频不存在或已被删除: aweme_id=%s", aweme_id)
        return {"status": "error", "error": "视频不存在或已被删除", "video_url": video_url}

    # 3. 提取无水印下载链接（优先级对齐 MediaCrawler:
    #    play_addr_h264 → play_addr_256 → play_addr 的 url_list[-1]，
    #    最后兜底 download_addr[0]）
    video_data = detail.get("video", {}) if isinstance(detail, dict) else {}
    media_url = ""
    for field in ("play_addr_h264", "play_addr_256", "play_addr"):
        url_list = (video_data.get(field, {}) or {}).get("url_list", []) or []
        if url_list:
            media_url = url_list[-1]
            break
    if not media_url:
        dl_list = (video_data.get("download_addr", {}) or {}).get("url_list", []) or []
        media_url = dl_list[0] if dl_list else ""
    logger.info("  下载链接: %s...", media_url[:80] if media_url else "EMPTY")

    if not media_url:
        if isinstance(detail, dict):
            logger.error("  detail keys: %s", list(detail.keys())[:10])
            if video_data:
                logger.error("  video keys: %s", list(video_data.keys())[:10])
        return {"status": "error", "error": "未找到可下载的视频链接", "video_url": video_url}

    # 4. 下载视频文件（需要 Referer + UA 模拟正常请求，流式写入 + 进度回调）
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
            async with http.stream("GET", media_url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and progress_callback:
                            pct = int(downloaded / total * 100)
                            progress_callback(pct)
                logger.info("  ✅ 下载完成: %d KB → %s", downloaded // 1024, target)
    except Exception as e:
        logger.error("  视频文件下载失败: %s", e)
        return {"status": "error", "error": f"视频文件下载失败: {e}", "video_url": video_url}

    if progress_callback:
        progress_callback(100)
    return {"status": "success", "local_path": str(target), "platform": "douyin", "resolved_url": video_url}


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
        """视频榜（CDP 页面内 XHR，webmssdk 自动签名）。"""
        from ._cdp_browser import run_on_cdp_loop_async
        return await run_on_cdp_loop_async(_fetch_hot_videos_via_page(limit))

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索（MediaCrawler CDP，提交到 CDP 专用循环执行）。"""
        from ._cdp_browser import run_on_cdp_loop_async
        return await run_on_cdp_loop_async(_search_via_cdp(keyword, limit))

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频（MediaCrawler CDP，提交到 CDP 专用循环执行）。"""
        from ._cdp_browser import run_on_cdp_loop_async
        return await run_on_cdp_loop_async(_get_creator_via_cdp(creator, limit))

    @staticmethod
    def download(video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        """下载抖音无水印视频（MediaCrawler CDP → httpx）。

        同步入口：提交到 CDP 专用常驻循环执行，避免 asyncio.run 临时循环
        与模块级 Playwright 单例的跨循环复用问题。
        """
        from ._cdp_browser import run_on_cdp_loop
        return run_on_cdp_loop(_download_via_cdp(video_url, file_name, progress_callback=progress_callback))


# 注册到全局注册表
register(DouyinPlatform)
