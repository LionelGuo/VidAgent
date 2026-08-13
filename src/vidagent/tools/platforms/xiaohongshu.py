"""小红书平台适配：MediaCrawler CDP 搜索 + 创作者 + 下载。

使用 XiaoHongShuClient (REST API + xhshow 签名)，与抖音共享浏览器管理模式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from typing import Any, Callable, ClassVar

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

_LOGIN_POLL_SECONDS = 120   # 扫码登录轮询上限


def _get_proxy() -> str | None:
    # xhs 为国内平台：直连（不走 youtube_proxy/clash）。
    # 外国出口 IP + 国内账号 cookie 会被风控静默拒绝
    # （MediaCrawler 官方同款行为：无 IP 代理时 proxy=None 直连）
    return None


# ---------------------------------------------------------------------------
# 客户端管理（通过共享 CDP 连接 Windows Chrome）
# ---------------------------------------------------------------------------

_client = None
_client_initialized = False


async def _ensure_client():
    global _client, _client_initialized

    if _client_initialized and _client is not None:
        return _client

    # 注意：xhs 模块无导入期 cwd 依赖（xhshow 签名纯算法、无文件访问），
    # MediaCrawler 根目录已由 _cdp_browser 加入 sys.path，此处无需 chdir。
    # 顺序约束：_cdp_browser 的 import 必须先于 `import config`——前者负责把
    # MediaCrawler 根加入 sys.path（首次调用时无此顺序依赖会 ModuleNotFoundError）
    from ._cdp_browser import get_page_for_platform, get_mc_utils  # noqa: F401

    import config as mc_config
    mc_config.PLATFORM = "xhs"
    mc_config.ENABLE_GET_MEIDAS = False
    mc_config.ENABLE_GET_COMMENTS = False

    from media_platform.xhs.client import XiaoHongShuClient

    _page = await get_page_for_platform("xhs", "https://www.xiaohongshu.com")

    ctx = _page.context
    cookie_str, cookie_dict = await get_mc_utils().convert_browser_context_cookies(
        ctx, urls=["https://www.xiaohongshu.com"],
    )

    # 请求头完全复刻 MediaCrawler create_xhs_client（core.py:361-392）——
    # 完整的浏览器式请求头是 xhs 风控放行的关键，缺任何一项都可能被静默拒绝
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": "https://www.xiaohongshu.com/",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie_str,
    }

    _client = XiaoHongShuClient(
        timeout=30, proxy=_get_proxy(), headers=headers,
        playwright_page=_page, cookie_dict=cookie_dict,
    )
    _client_initialized = True
    logger.info("XiaoHongShuClient 已就绪 (CDP)")

    # 登录态检查（MediaCrawler 同款 pong() 判定），未登录引导扫码
    if not await client_pong_safe(_client):
        await _guide_login(_page, _client)

    return _client


async def client_pong_safe(client) -> bool:
    """pong() 登录态检查（防异常挂起）。"""
    try:
        return bool(await client.pong())
    except Exception as e:
        logger.warning("pong() 检查失败: %s", e)
        return False


async def _guide_login(page, client) -> None:
    """在 CDP 页面上引导用户扫码登录（最多 120s）。"""
    logger.info("小红书未登录 — 在浏览器页面引导扫码登录（最多 %ds）…", _LOGIN_POLL_SECONDS)
    try:
        try:
            await page.goto(
                "https://www.xiaohongshu.com/",
                wait_until="domcontentloaded", timeout=15000,
            )
        except Exception:
            pass
        try:
            login_btn = page.locator(
                "xpath=//div[contains(@class,'login-btn')] | //span[text()='登录']"
            )
            await login_btn.first.click(timeout=5000)
        except Exception:
            pass  # 登录弹窗可能已自动出现
    except Exception as e:
        logger.warning("登录弹窗触发失败（不影响轮询）: %s", e)

    for _ in range(_LOGIN_POLL_SECONDS):
        await asyncio.sleep(1)
        try:
            if await client_pong_safe(client):
                await client.update_cookies(
                    page.context, urls=["https://www.xiaohongshu.com"],
                )
                logger.info("小红书扫码登录成功")
                return
        except Exception:
            pass
    logger.warning("小红书登录等待超时（%ds），继续尝试", _LOGIN_POLL_SECONDS)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    m = _XHS_NOTE_RE.search(url)
    return m.group(1) or m.group(2) if m else None


def _safe_int(v: Any) -> int:
    """安全整数转换：xhs 字段多为字符串/可空（"" 或 None 会令 int() 抛异常）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def normalize(item: dict) -> dict:
    note = item.get("note_card", item) or item
    # note_id / xsec_token 在搜索结果的 item 顶层（MediaCrawler 官方
    # core.py 用 post_item.get("id") / post_item.get("xsec_token")）
    note_id = str(item.get("id", "") or note.get("note_id", "") or note.get("id", ""))
    xsec_token = item.get("xsec_token", "") or note.get("xsec_token", "")
    xsec_source = item.get("xsec_source", "") or note.get("xsec_source", "")

    title = note.get("display_title", "") or note.get("title", "")
    desc = note.get("desc", "") or ""
    author_info = note.get("user", {}) or {}
    author_name = author_info.get("nickname", "") if isinstance(author_info, dict) else ""
    stats = note.get("interact_info", {}) or {}
    # 视频时长（feed API 的 video 结构不含 duration 字段，通常为 0，
    # 下载后由 server 层 ffprobe 补充）
    video_info = note.get("video", {}) or {}
    duration_sec = _safe_int(video_info.get("duration", 0)) if video_info else 0
    # 图片笔记无时长
    if not duration_sec and note.get("type") == "video" and video_info:
        duration_sec = _safe_int(video_info.get("video_duration", 0))
    create_time = _safe_int(note.get("time", 0))

    # video_url 附带 xsec_token，下载时可直接解析（分享 URL 同构）
    video_url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
    if video_url and xsec_token:
        video_url += f"?xsec_token={xsec_token}"
        if xsec_source:
            video_url += f"&xsec_source={xsec_source}"

    return {
        "video_id": note_id,
        "title": title[:200] if title else (desc[:200] if desc else ""),
        "desc": desc[:500] if desc else "",
        "publish_time": create_time,
        "duration": duration_sec,
        # duration 未知（feed API 不含时长）时留空，前端隐藏时长徽标；
        # 下载后 server 层会 ffprobe 补充
        "duration_text": _fmt_duration(duration_sec) if duration_sec else "",
        "video_url": video_url,
        "platform": "xiaohongshu",
        "author": author_name,
        "view_count": _safe_int(stats.get("liked_count", 0)),
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
    try:
        client = await _ensure_client()
    except Exception as e:
        # CDP 连不上 Windows Chrome（对齐抖音搜索的优雅降级：不 500）
        logger.warning("小红书搜索失败（无法连接 Windows Chrome 调试端口 9222）: %s", e)
        return []
    try:
        # 参数对齐 MediaCrawler 官方 search()（core.py:286-297）：
        # 显式 search_id + 默认 page_size=20（不传小 page_size）
        # note_type=VIDEO：服务端只返回视频笔记（对齐网页端「视频」tab，
        # SearchNoteType.VIDEO=1，见 MediaCrawler field.py:65-72）
        from media_platform.xhs.help import get_search_id
        from media_platform.xhs.field import SearchNoteType
        resp = await client.get_note_by_keyword(
            keyword=keyword,
            search_id=get_search_id(),
            page=1,
            note_type=SearchNoteType.VIDEO,
        )
    except Exception as e:
        err_msg = str(e)
        if "DataFetchError" in err_msg:
            logger.warning("小红书搜索失败（API 拒接，可能未登录或风控）: %s", err_msg[:120])
        else:
            # tenacity RetryError 的 str 不含底层原因，展开 last_attempt
            cause = getattr(e, "last_attempt", None)
            underlying = cause.exception() if cause is not None else e
            logger.warning("小红书搜索失败: %r", underlying)
        return []

    items = resp.get("items", []) if isinstance(resp, dict) else []
    if not items:
        # 空结果诊断：转储响应键与浏览器 cookie 名（静默风控的典型信号）
        try:
            ctx_cookies = await client.playwright_page.context.cookies(
                "https://www.xiaohongshu.com")
            logger.warning(
                "xhs 搜索空结果: resp_keys=%s cookie_names=%s",
                list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__,
                sorted(c["name"] for c in ctx_cookies),
            )
        except Exception:
            logger.warning("xhs 搜索空结果: resp_keys=%s",
                           list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
    # 过滤推广卡片（对齐 MediaCrawler core.py:168：rec_query/hot_query 非真实笔记，
    # 其 id 为 UUID 格式，无法下载）
    items = [
        it for it in items
        if isinstance(it, dict) and it.get("model_type") not in ("rec_query", "hot_query")
    ]
    items = items[:limit]

    # 二次查详情（官方 get_note_detail_async_task 做法）：搜索响应的 note_card
    # 不含 type/video/duration 字段，按 note_id 逐个查询后图文/视频类型与时长才准确
    _diag_done = False

    async def _fetch_detail(item: dict) -> dict:
        nonlocal _diag_done
        note_id = str(item.get("id") or "")
        xsec_source = item.get("xsec_source", "")
        xsec_token = item.get("xsec_token", "")
        if not note_id:
            return item
        try:
            detail = await client.get_note_by_id(note_id, xsec_source, xsec_token)
            if isinstance(detail, dict) and detail:
                # 官方 core.py:308：详情数据不含 xsec_token，从搜索 item 回填
                detail.setdefault("xsec_token", xsec_token)
                detail.setdefault("xsec_source", xsec_source)
                if not _diag_done:
                    _diag_done = True
                    src_note = item.get("note_card", item) or {}
                    src_video = src_note.get("video") if isinstance(src_note, dict) else None
                    det_video = detail.get("video")
                    logger.warning(
                        "xhs 诊断: 搜索item keys=%s video_keys=%s | feed详情 video_keys=%s type=%s",
                        sorted(item.keys()),
                        sorted(src_video.keys()) if isinstance(src_video, dict) else type(src_video).__name__,
                        sorted(det_video.keys()) if isinstance(det_video, dict) else type(det_video).__name__,
                        detail.get("type"),
                    )
                return detail
            logger.warning("小红书详情查询无结果 %s (返回 %s, src=%s)",
                           note_id, type(detail).__name__, xsec_source)
        except Exception as e:
            logger.warning("小红书详情查询异常 %s: %s", note_id, str(e)[:120])
        return item  # 回退：用搜索原始 item（type/duration 可能缺失）

    sem = asyncio.Semaphore(3)  # 限制并发，控制风控风险

    async def _limited(item: dict) -> dict:
        async with sem:
            return await _fetch_detail(item)

    try:
        detailed = await asyncio.wait_for(
            asyncio.gather(*[_limited(it) for it in items]), timeout=30,
        )
    except asyncio.TimeoutError:
        logger.warning("小红书详情批量查询超时（30s），使用搜索原始数据")
        detailed = items

    results = [normalize(it) for it in detailed]
    logger.info("🔍 小红书搜索 '%s': %d 条", keyword, len(results))
    # 对齐官方搜索后节流（base_config.py: CRAWLER_MAX_SLEEP_SEC=2），
    # 连续密集请求是触发风控的主因之一
    await asyncio.sleep(random.uniform(2, 4))
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


async def _download_via_cdp(note_url: str, file_name: str,
                            progress_callback=None) -> dict:
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        if progress_callback:
            progress_callback(100)
        return {"status": "success", "local_path": str(target), "platform": "xiaohongshu", "cached": True}

    # 1. 解析 note_id + xsec_token/xsec_source（分享 URL 自带，裸 URL 则留空走兜底）
    xsec_token, xsec_source = "", ""
    try:
        from media_platform.xhs.help import parse_note_info_from_note_url
        note_info = parse_note_info_from_note_url(note_url)
        note_id = note_info.note_id
        xsec_token = note_info.xsec_token
        xsec_source = note_info.xsec_source
    except Exception:
        note_id = extract_video_id(note_url)
    if not note_id:
        return {"status": "error", "error": f"无法解析小红书笔记 ID: {note_url}", "video_url": note_url}

    client = await _ensure_client()

    # 2. 笔记详情：API（带 token）→ 短链接口 → HTML 兜底
    note_card = None
    note_source = ""
    if xsec_token:
        try:
            note_card = await client.get_note_by_id(note_id, xsec_source, xsec_token)
            note_source = "feed API"
        except Exception as e:
            logger.warning("get_note_by_id 失败: %s", e)
    if not note_card:
        try:
            short_resp = await client.get_note_short_url(note_id)
            items = short_resp.get("items", []) if isinstance(short_resp, dict) else []
            if items:
                note_card = items[0].get("note_card", items[0])
                note_source = "short_url"
        except Exception as e:
            logger.warning("get_note_short_url 失败: %s", e)
    if not note_card:
        try:
            note_card = await client.get_note_by_id_from_html(note_id, xsec_source, xsec_token)
            note_source = "html"
        except Exception as e:
            logger.warning("get_note_by_id_from_html 兜底失败: %s", e)
    if not note_card:
        return {"status": "error", "error": "小红书笔记获取失败（可能需登录或笔记不可访问）", "video_url": note_url}

    logger.info(
        "  笔记来源: %s, type=%s, keys=%s, video_keys=%s",
        note_source, note_card.get("type"),
        sorted(k for k in note_card.keys())[:12],
        sorted(k for k in (note_card.get("video") or {}).keys()) if isinstance(note_card.get("video"), dict) else type(note_card.get("video")).__name__,
    )

    if note_card.get("type") != "video":
        # fatal：图文笔记是确定性结果，重试无意义（server 层不再重试）
        return {"status": "error", "fatal": True,
                "error": "该笔记为图文笔记，暂不支持视频总结", "video_url": note_url}

    return await _download_note_media(note_card, target, progress_callback)


def _extract_h264_url(container: Any) -> str:
    """从 media/media_v2 容器中提取 h264 流 URL（类型安全）。"""
    if not isinstance(container, dict):
        return ""
    stream = container.get("stream")
    if not isinstance(stream, dict):
        return ""
    h264 = stream.get("h264")
    if isinstance(h264, list) and h264 and isinstance(h264[0], dict):
        return h264[0].get("master_url", "") or ""
    # 兜底：任意清晰度流
    for entries in stream.values():
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            url = (entries[0].get("master_url", "")
                   or entries[0].get("url", "")
                   or (entries[0].get("backup_urls") or [""])[0])
            if url:
                return url
    return ""


async def _download_note_media(note: dict, target: Path,
                               progress_callback=None) -> dict:
    """从笔记数据中提取并下载视频文件。

    优先级：consumer.origin_video_key（无水印直链）→
    media_v2/media 的 h264 master_url（带水印兜底）。
    """
    video_info = note.get("video", {})
    if not isinstance(video_info, dict):
        video_info = {}
    consumer = video_info.get("consumer", {})
    if not isinstance(consumer, dict):
        consumer = {}
    origin_video_key = consumer.get("origin_video_key") or consumer.get("originVideoKey") or ""
    media_url = f"http://sns-video-bd.xhscdn.com/{origin_video_key}" if origin_video_key else ""

    if not media_url:
        # h264 流：media_v2（新版 API 字段）→ media（旧版字段）
        for field in ("media_v2", "media"):
            media_url = _extract_h264_url(video_info.get(field))
            if media_url:
                break

    if not media_url:
        for field in ("media_v2", "media"):
            v = video_info.get(field)
            logger.warning(
                "  视频 URL 结构诊断[%s]: type=%s value=%s",
                field, type(v).__name__,
                (json.dumps(v, ensure_ascii=False)[:400]
                 if isinstance(v, (dict, list)) else str(v)[:200]),
            )
        return {"status": "error", "error": "未找到可下载的视频链接"}

    try:
        async with httpx.AsyncClient(
            proxy=_get_proxy(), timeout=120, follow_redirects=True,
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
                            progress_callback(int(downloaded / total * 100))
    except Exception as e:
        return {"status": "error", "error": f"视频文件下载失败: {e}"}

    if progress_callback:
        progress_callback(100)
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
        raise NotImplementedError(
            "小红书暂无热榜视频接口（网页端热搜为话题词形态，非视频列表），"
            "请改用关键词搜索（search_videos），例如「搜索小红书的 vlog」"
        )

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        from ._cdp_browser import run_on_cdp_loop_async
        return await run_on_cdp_loop_async(_search_via_cdp(keyword, limit))

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        from ._cdp_browser import run_on_cdp_loop_async
        return await run_on_cdp_loop_async(_get_creator_via_cdp(creator, limit))

    @staticmethod
    def download(video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        from ._cdp_browser import run_on_cdp_loop
        return run_on_cdp_loop(_download_via_cdp(video_url, file_name, progress_callback=progress_callback))


register(XiaohongshuPlatform)
