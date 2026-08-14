"""快手平台适配：CDP 页面监听方案（搜索 + 创作者 + 下载）。

2026-08-13 调研结论（严格参考 MediaCrawler 源码后的升级，红线：不做
MediaCrawler 未覆盖的自定义签名逆向）：
- MediaCrawler 官方 GraphQL 方案（visionSearchPhoto / visionVideoDetail，
  media_platform/kuaishou/client.py）已被快手废弃：GraphQL 返回
  result=50（接口下线业务码）。
- 网页端现用 REST API：/rest/v/search/feed、/rest/v/profile/feed 等，
  URL 带 __NS_hxfalcon 页面 JS 签名，服务端强制校验（CDP 实测无签名
  fetch 返回 {"result":50,"error_msg":"签名验证失败"}）。
- 因此采用「页面自己发请求，我们监听响应」：导航页面触发页面 JS 自动
  发起真实搜索请求（签名由页面生成，与真实浏览行为完全一致），通过
  Playwright expect_response 被动读取返回 JSON。翻页靠滚动触发加载。
- 视频详情页无数据 XHR（SSR 渲染）：数据在 window.__APOLLO_STATE__，
  下载直链 photoUrl 在 VisionVideoDetailPhoto:<id> 节点（含实时 tag）。
- URL 解析对齐官方 media_platform/kuaishou/help.py（parse_video_info_from_url /
  parse_creator_info_from_url）。
- 无代理直连（国内平台，走代理有风控风险）；搜索后节流 2s
  （对齐官方 CRAWLER_MAX_SLEEP_SEC=2 精神）。

设计要点（与 douyin.py 同，见 docs/adr/0004）：
- 页面来自共享 CDP 设施（_cdp_browser.get_page_for_platform），复用
  用户 Windows Chrome 登录态；页面失效自动重建。
- 绝不关闭 CDP 浏览器 context——那是用户的真实浏览器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import quote

import httpx
from playwright.async_api import TimeoutError as PWTimeoutError

from vidagent.tools.platforms import register

from ._cdp_browser import invalidate_page
from ._mediacrawler import MediaCrawlerPlatform

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.kuaishou.com/",
}

# 官方 help.py 正则：/short-video/([a-zA-Z0-9_-]+)
_KS_VIDEO_ID_RE = re.compile(r"/short-video/([a-zA-Z0-9_-]+)")

_KS_PAGE_SIZE = 20        # 页面每屏返回条数（实测 19-20）
# 注意：Playwright expect_response 的 timeout 单位是毫秒
_PAGE_TIMEOUT_MS = 15000  # 页面自动请求的等待上限
_SCROLL_TIMEOUT_MS = 10000  # 滚动翻页的等待上限
_MAX_PAGES = 5            # 翻页上限（防无限滚动）
_APOLLO_POLL_SECONDS = 15  # 详情页 Apollo 数据就绪轮询上限
_SLEEP_SEC = 2            # 搜索/创作者后节流（对齐官方 CRAWLER_MAX_SLEEP_SEC）

CDP_GUIDE_MSG = (
    "无法连接 Windows Chrome 调试端口 9222。请确认："
    "1) Chrome 已运行并开启远程调试（chrome://inspect/#remote-debugging 勾选，"
    "或命令行 --remote-debugging-port=9222 启动）；"
    "2) 浏览器弹出的调试连接确认框已点「接受」。"
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class KuaishouClientError(RuntimeError):
    """快手 CDP 页面不可用（连接失败 / 页面异常）。"""


# ---------------------------------------------------------------------------
# MediaCrawler 模块一次性导入（仅 URL 解析，无 client/graphql 依赖）
# ---------------------------------------------------------------------------

_ParseVideoInfo: Any = None
_ParseCreatorInfo: Any = None


def _import_mediacrawler() -> None:
    """导入 MediaCrawler 快手 URL 解析（官方 help.py，首次调用时执行）。

    chdir 语义由门户 import_mc_platform 负责（kuaishou 包 __init__ 会级联
    core/client，门户只导入 help 子模块，不拖 GraphQL 链）。
    """
    global _ParseVideoInfo, _ParseCreatorInfo
    if _ParseVideoInfo is not None:
        return

    mods = KuaishouPlatform.import_mc()
    _ParseVideoInfo = mods["help"].parse_video_info_from_url
    _ParseCreatorInfo = mods["help"].parse_creator_info_from_url

    logger.info("MediaCrawler 快手 URL 解析已导入")


# ---------------------------------------------------------------------------
# 页面管理（#3：共享 CDP 设施；串行锁在 KuaishouPlatform.mc_lock）
# ---------------------------------------------------------------------------


async def _get_page(url: str) -> Any:
    """获取快手 CDP 页面（失效自动重建，绝不关闭用户浏览器）。

    调用点均持 mc_lock（平台锁内注入，见 _mediacrawler 契约）。
    """
    KuaishouPlatform.inject_config()
    try:
        return await KuaishouPlatform.get_page(url)
    except RuntimeError:
        raise KuaishouClientError(CDP_GUIDE_MSG) from None
    except Exception as e:
        await invalidate_page(KuaishouPlatform.cdp_page_key)
        raise KuaishouClientError(f"快手页面加载失败: {e}") from e


async def _login_cookie_names(page: Any) -> list[str]:
    """诊断：当前页面的快手登录相关 cookie（passToken 为官方 login.py 判定）。"""
    try:
        cookies = await page.context.cookies()
        return sorted(
            c["name"] for c in cookies
            if c.get("name") in ("passToken", "kuaishou.server.web_st")
            and c.get("value")
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    m = _KS_VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _safe_int(v: Any) -> int:
    """安全整数转换：防御 None / 空串 / 非数字字符串。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def normalize(item: dict) -> dict:
    """字段映射对齐 MediaCrawler store/kuaishou/__init__.py。

    注意：feed 的 photo.duration / photo.timestamp 均为毫秒（实测
    duration=167266ms 即 167s 视频），viewCount 为数字。
    """
    photo = item.get("photo", item) or item
    video_id = str(photo.get("id", ""))
    caption = photo.get("caption", "") or ""
    author_info = item.get("author", {}) or {}
    author_name = author_info.get("name", "") if isinstance(author_info, dict) else ""
    duration_sec = _safe_int(photo.get("duration", 0)) // 1000
    return {
        "video_id": video_id,
        "title": caption[:200] if caption else "",
        "desc": caption[:500] if caption else "",
        "publish_time": _safe_int(photo.get("timestamp", 0)) // 1000,
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec) if duration_sec else "",
        "video_url": f"https://www.kuaishou.com/short-video/{video_id}" if video_id else "",
        "platform": "kuaishou",
        "author": author_name,
        "view_count": _safe_int(photo.get("viewCount", 0)),
    }


def _fmt_duration(sec: int) -> str:
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 页面响应收集（页面自己发请求，我们被动读取——不做自定义签名请求）
# ---------------------------------------------------------------------------

async def _collect_feed_responses(
    page: Any,
    goto_coro: Any,
    url_substr: str,
    limit: int,
) -> list[dict]:
    """导航触发页面自动请求，监听响应收集 feeds（含滚动翻页）。

    页面 JS 生成 __NS_hxfalcon 签名自行请求，这里仅通过 expect_response
    被动读取响应——零自定义请求，风控行为与真实浏览一致。
    """
    def _pred(resp: Any) -> bool:
        return url_substr in resp.url

    feeds: list[dict] = []

    # 1. 首屏：导航 → 页面自动发起带签名的搜索/列表请求
    try:
        async with page.expect_response(_pred, timeout=_PAGE_TIMEOUT_MS) as ri:
            await goto_coro
        body = await (await ri.value).text()
        data = json.loads(body)
    except PWTimeoutError:
        login_cookies = await _login_cookie_names(page)
        logger.warning(
            "快手页面数据响应未捕获(%ds 超时): %s | 登录cookie=%s(可能未登录或风控)",
            _PAGE_TIMEOUT_MS // 1000, url_substr, login_cookies,
        )
        return []
    except Exception as e:
        logger.warning("快手页面数据响应异常: %s", e)
        return []

    if not isinstance(data, dict) or data.get("result") != 1:
        logger.warning(
            "快手页面数据异常: url=%s result=%s",
            url_substr, data.get("result") if isinstance(data, dict) else type(data).__name__,
        )
        return []
    feeds.extend(data.get("feeds", []) or [])

    # 2. 滚动翻页凑 limit（页面 IntersectionObserver 触发加载更多）
    pages = 1
    while len(feeds) < limit and pages < _MAX_PAGES:
        try:
            async with page.expect_response(_pred, timeout=_SCROLL_TIMEOUT_MS) as ri:
                await page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)",
                )
            body = await (await ri.value).text()
            batch = (json.loads(body) or {}).get("feeds", []) or []
        except PWTimeoutError:
            break  # 无更多数据或已到底
        except Exception as e:
            logger.warning("快手翻页响应异常: %s", e)
            break
        if not batch:
            break
        feeds.extend(batch)
        pages += 1

    return feeds[:limit]


async def _collect_user_responses(page: Any, goto_coro: Any) -> list[dict]:
    """导航搜索页，监听页面自身的 /rest/v/search/user 响应收集用户列表。

    与 _collect_feed_responses 同法：页面 JS 生成 __NS_hxfalcon 签名自行请求，
    仅被动读取响应——零自定义请求。
    """
    def _pred(resp: Any) -> bool:
        return "/rest/v/search/user" in resp.url

    try:
        async with page.expect_response(_pred, timeout=_PAGE_TIMEOUT_MS) as ri:
            await goto_coro
        body = await (await ri.value).text()
        data = json.loads(body)
    except PWTimeoutError:
        logger.warning("快手用户搜索响应未捕获(超时)")
        return []
    except Exception as e:
        logger.warning("快手用户搜索响应异常: %s", e)
        return []
    if not isinstance(data, dict) or data.get("result") != 1:
        logger.warning(
            "快手用户搜索数据异常: result=%s",
            data.get("result") if isinstance(data, dict) else type(data).__name__,
        )
        return []
    return data.get("users", []) or []


# ---------------------------------------------------------------------------
# 搜索 + 创作者
# ---------------------------------------------------------------------------

async def _search_via_cdp(keyword: str, limit: int = 10) -> list[dict]:
    """关键词搜索：导航搜索页，监听页面自身的 /rest/v/search/feed 响应。"""
    async with KuaishouPlatform.mc_lock:
        page = await _get_page("https://www.kuaishou.com?isHome=1")
        search_url = (
            "https://www.kuaishou.com/search/video?searchKey=" + quote(keyword)
        )
        try:
            feeds = await _collect_feed_responses(
                page,
                goto_coro=page.goto(search_url, wait_until="domcontentloaded", timeout=15000),
                url_substr="/rest/v/search/feed",
                limit=limit,
            )
        except Exception as e:
            logger.warning("快手搜索失败: %s", e)
            feeds = []

        results = [normalize(f) for f in feeds]
        logger.info("快手搜索 '%s': %d 条", keyword, len(results))
        await asyncio.sleep(_SLEEP_SEC)  # 节流（官方 CRAWLER_MAX_SLEEP_SEC）
        return results


_KS_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")  # user_id 字符集（官方 URL 正则同款）


async def _resolve_creator_user_id(creator: str) -> str | None:
    """解析 creator 输入为 user_id。

    - URL（kuaishou.com/profile/xxx）→ 官方 parse_creator_info_from_url
    - 纯 user_id（`[a-zA-Z0-9_-]+`，官方 URL 正则同款字符集）→ 直接使用
    - 其它（昵称，如中文）→ 按昵称搜索用户（导航搜索页监听页面自身的
      /rest/v/search/user 响应，取第一条（平台相关度排序））
    """
    if "kuaishou.com" in creator or creator.startswith("http"):
        try:
            return _ParseCreatorInfo(creator).user_id
        except (ValueError, AttributeError):
            return None
    if _KS_USER_ID_RE.match(creator):
        return creator

    async with KuaishouPlatform.mc_lock:
        page = await _get_page("https://www.kuaishou.com?isHome=1")
        users = await _collect_user_responses(
            page,
            goto_coro=page.goto(
                f"https://www.kuaishou.com/search/video?searchKey={quote(creator)}",
                wait_until="domcontentloaded", timeout=15000,
            ),
        )
    if not users:
        return creator  # 用户搜索无结果：当作纯 user_id 兜底
    # 直接取第一条（平台相关度排序）——勿做精确昵称匹配优先：同名小号会
    # 覆盖平台排在前面的知名账号（2026-08-14 xhs 张朝阳 教训）
    best = users[0]
    user_id = best.get("user_id") if isinstance(best, dict) else None
    if user_id:
        logger.info("快手昵称 '%s' -> 用户 '%s' (%s)", creator, best.get("user_name"), user_id)
    else:
        logger.warning(
            "快手用户搜索条目无 user_id: keys=%s",
            sorted(best.keys())[:10] if isinstance(best, dict) else type(best).__name__,
        )
    return user_id


async def _get_creator_via_cdp(creator: str, limit: int = 10) -> list[dict]:
    """创作者视频：URL → 官方解析；昵称 → 用户搜索解析 user_id；
    导航主页监听页面自身的 /rest/v/profile/feed 响应。"""
    from ._cdp_browser import check_mediacrawler_available
    if (msg := check_mediacrawler_available()) is not None:
        logger.warning(msg)
        return []
    _import_mediacrawler()
    user_id = await _resolve_creator_user_id(creator)
    if not user_id:
        logger.warning("快手创作者解析失败: '%s'", creator)
        return []

    async with KuaishouPlatform.mc_lock:
        page = await _get_page("https://www.kuaishou.com?isHome=1")
        profile_url = f"https://www.kuaishou.com/profile/{quote(user_id)}"
        try:
            feeds = await _collect_feed_responses(
                page,
                goto_coro=page.goto(profile_url, wait_until="domcontentloaded", timeout=15000),
                url_substr="/rest/v/profile/feed",
                limit=limit,
            )
        except Exception as e:
            logger.warning("快手创作者查询失败: %s", e)
            feeds = []

        results = [normalize(f) for f in feeds]
        logger.info("快手创作者 %s: %d 个视频", user_id, len(results))
        await asyncio.sleep(_SLEEP_SEC)  # 节流（官方 CRAWLER_MAX_SLEEP_SEC）
        return results


# ---------------------------------------------------------------------------
# 下载（详情页 SSR → Apollo 缓存 → photoUrl 直链）
# ---------------------------------------------------------------------------

_APOLLO_PHOTO_JS = """(photoId) => {
  const dc = (window.__APOLLO_STATE__ || {}).defaultClient;
  if (!dc) return null;
  const p = dc['VisionVideoDetailPhoto:' + photoId];
  if (!p || !p.photoUrl) return null;
  return {photoUrl: p.photoUrl, caption: p.caption || '', duration: p.duration, timestamp: p.timestamp};
}"""


async def _wait_detail_photo(page: Any, photo_id: str) -> dict | None:
    """等详情页 SSR 数据（window.__APOLLO_STATE__）就绪并取出 photo 节点。"""
    for _ in range(_APOLLO_POLL_SECONDS * 2):
        try:
            photo = await page.evaluate(_APOLLO_PHOTO_JS, photo_id)
            if photo:
                return photo
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def _download_via_cdp(video_url: str, file_name: str,
                            progress_callback: Callable[[int], None] | None = None) -> dict:
    """详情页提取 photoUrl（实时 tag 直链），httpx 流式下载。"""
    from ._cdp_browser import check_mediacrawler_available
    if (msg := check_mediacrawler_available()) is not None:
        return {"status": "error", "error": msg, "video_url": video_url}
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        logger.info("下载命中缓存: %s", target)
        if progress_callback:
            progress_callback(100)
        return {"status": "success", "local_path": str(target), "platform": "kuaishou", "cached": True}

    logger.info("快手下载开始: url=%s", video_url)

    # 1. 解析 photo_id（官方 help.py parse_video_info_from_url → 内置正则兜底）
    _import_mediacrawler()
    try:
        video_info = _ParseVideoInfo(video_url)
        photo_id = video_info.video_id
    except Exception:
        photo_id = extract_video_id(video_url)
    if not photo_id:
        return {"status": "error", "error": f"无法解析快手视频 ID: {video_url}", "video_url": video_url}
    photo_id = str(photo_id)

    # 2. 详情页：SSR 数据在 window.__APOLLO_STATE__（页面无数据 XHR）
    detail_url = f"https://www.kuaishou.com/short-video/{photo_id}"
    logger.info("获取详情页数据: photo_id=%s", photo_id)
    async with KuaishouPlatform.mc_lock:
        page = await _get_page(detail_url)
        try:
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            logger.warning("详情页导航异常(继续等数据): %s", e)
        photo = await _wait_detail_photo(page, photo_id)

    if not photo:
        login_cookies = await _login_cookie_names(page)
        logger.error(
            "详情页数据未就绪: photo_id=%s 登录cookie=%s(可能未登录/被删/风控)",
            photo_id, login_cookies,
        )
        return {"status": "error", "error": "快手详情页数据未就绪（可能未登录或视频不可访问）", "video_url": video_url}

    play_url = photo.get("photoUrl", "")
    if not play_url:
        return {"status": "error", "error": "未找到可下载的视频链接", "video_url": video_url}
    logger.info("下载链接: %s...", play_url[:80])

    # 3. 下载视频文件（UA + Referer 模拟正常请求，直连，流式写入 + 进度回调）
    try:
        dl_headers = {
            **DEFAULT_HEADERS,
            "Referer": detail_url,
        }
        part = None  # 先写 .part 再原子替换：流中断的残片不得命中缓存
        async with httpx.AsyncClient(
            headers=dl_headers, timeout=120, follow_redirects=True,
        ) as http:
            async with http.stream("GET", play_url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                target.parent.mkdir(parents=True, exist_ok=True)
                part = target.with_name(target.name + ".part")
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and progress_callback:
                            progress_callback(int(downloaded / total * 100))
                os.replace(part, target)
        logger.info("下载完成: %d KB -> %s", downloaded // 1024, target)
    except Exception as e:
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
        logger.error("视频文件下载失败: %s", e)
        return {"status": "error", "error": f"视频文件下载失败: {e}", "video_url": video_url}

    if progress_callback:
        progress_callback(100)
    return {"status": "success", "local_path": str(target), "platform": "kuaishou"}


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class KuaishouPlatform(MediaCrawlerPlatform):
    name: ClassVar[str] = "kuaishou"
    aliases: ClassVar[tuple[str, ...]] = ("ks", "快手")
    url_patterns: ClassVar[tuple[str, ...]] = ("kuaishou.com", "chenzhongtech.com")

    # -- MediaCrawlerPlatform 声明（#3） --
    # 无 client（页面监听方案）：不用 ensure_client 模板，只走门户 + 锁 + page 设施
    cdp_page_key: ClassVar[str] = "ks"
    mc_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    mc_submodules: ClassVar[tuple[str, ...]] = ("help",)
    mc_package: ClassVar[str] = "kuaishou"

    # -- 检索 / 下载 --

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return extract_video_id(url)

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout)

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 20) -> list[dict]:
        raise NotImplementedError(
            "快手暂不支持热榜（MediaCrawler 无快手热榜实现，为避免风控不做自定义接口），"
            "请改用关键词搜索（search_videos）"
        )

    @classmethod
    async def search(cls, client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索（CDP 页面监听，提交到 CDP 专用循环执行）。"""
        return await cls.run_on_cdp_async(_search_via_cdp(keyword, limit))

    @classmethod
    async def get_creator(cls, client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频（CDP 页面监听，提交到 CDP 专用循环执行）。"""
        return await cls.run_on_cdp_async(_get_creator_via_cdp(creator, limit))

    @classmethod
    def download(cls, video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        """下载快手视频（详情页 Apollo 数据 → httpx）。

        同步入口：提交到 CDP 专用常驻循环执行，避免 asyncio.run 临时循环
        与模块级 Playwright 单例的跨循环复用问题。
        """
        return cls.run_on_cdp(_download_via_cdp(video_url, file_name, progress_callback=progress_callback))


register(KuaishouPlatform)
