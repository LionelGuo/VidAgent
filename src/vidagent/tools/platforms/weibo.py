"""微博平台适配（#9，2026-08-15）：MediaCrawler CDP 登录态 + httpx 直连 m.weibo.cn。

数据路径：搜索/创作者/详情全部走 MediaCrawler WeiboClient（上游 @1779dde
原样恢复进 vendor，httpx 直连 m.weibo.cn）；CDP 仅承担登录态（cookie 采集
+ 扫码引导）——与 #3 Q5 预判「httpx 直连、CDP 仅登录」一致。

风控纪律（照搬上游设计 + 本项目约束）：
- 微博无签名机制（无 XS-TOKEN），风控靠登录 cookie + 移动 UA +
  Origin/Referer 头 + 固定 2s 请求间隔（CRAWLER_MAX_SLEEP_SEC）。
- **登录是硬门槛**（douyin 同款）：未登录请求 m.weibo.cn 基本必触发
  验证码——未登录时零 API 请求，直接引导扫码。
- 上游自述 ENABLE_WEIBO_FULL_TEXT=True 会增加被风控概率 → 注入 False。
- 搜索/创作者只取第一页，不翻页（douyin 翻页被 ArgusSecurityPlugin 拦的
  教训；weibo client 内建的 request 重试〔432 时刷新 cookie〕是上游自带
  的风控恢复机制，保留不动）。
- 昵称找人走 CDP 页面内 XHR（douyin usersearch 同款）：页面 JS 上下文
  请求与真实浏览一致，不新增直连签名面。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import httpx

from vidagent.tools.platforms import register

from ._cdp_browser import invalidate_page
from ._mediacrawler import MediaCrawlerPlatform

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_WB_DETAIL_RE = re.compile(r"m\.weibo\.cn/detail/(\w+)")
_WB_STATUS_ONLY_RE = re.compile(r"weibo\.com/status/(\w+)")
_WB_STATUS_RE = re.compile(r"weibo\.com/\d+/(\w+)")
_WB_CN_RE = re.compile(r"weibo\.cn/(\w+)")
_WB_PROFILE_RE = re.compile(r"m\.weibo\.cn/profile/(\d+)")
_WB_U_RE = re.compile(r"weibo\.com/u/(\d+)")
_WB_UID_PATH_RE = re.compile(r"weibo\.com/(\d+)(?:/|\?|$)")
_WB_CN_UID_RE = re.compile(r"weibo\.cn/(\d+)")

_CLIENT_TIMEOUT = 60        # WeiboClient 单请求 HTTP 超时（上游默认，图片长超时）
_CALL_TIMEOUT = 60          # 单次 client 调用上限（request 内建重试 5×3s，需留余量）
_LOGIN_POLL_SECONDS = 120   # 扫码登录轮询上限

CDP_GUIDE_MSG = (
    "无法连接 Windows Chrome 调试端口 9222。请确认："
    "1) Chrome 已运行并开启远程调试（chrome://inspect/#remote-debugging 勾选，"
    "或命令行 --remote-debugging-port=9222 启动）；"
    "2) 浏览器弹出的调试连接确认框已点「接受」。"
)

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class WeiboClientError(RuntimeError):
    """微博客户端不可用（CDP 连接失败 / 页面异常）。"""


class WeiboLoginError(WeiboClientError):
    """微博未登录且扫码引导失败。"""


# ---------------------------------------------------------------------------
# MediaCrawler 模块一次性导入（#3：chdir 语义与缓存由 _mediacrawler 门户负责）
# ---------------------------------------------------------------------------

_SearchType: Any = None
_FilterSearchCard: Any = None


def _import_mediacrawler() -> None:
    """导入 MediaCrawler 微博模块（首次调用时执行）。"""
    global _SearchType, _FilterSearchCard
    if _SearchType is not None:
        return

    mods = WeiboPlatform.import_mc()
    _SearchType = mods["field"].SearchType
    _FilterSearchCard = mods["help"].filter_search_result_card

    logger.info("MediaCrawler 微博模块已导入")


def _get_proxy() -> str | None:
    # weibo 为国内平台：直连（不走 youtube_proxy/clash），镜像 xhs/douyin 先例。
    # 外国出口 IP + 国内账号 cookie 会被风控静默拒绝。
    return None


# ---------------------------------------------------------------------------
# 登录态判定 / 扫码引导（CDP 页面，零 API 请求）
# ---------------------------------------------------------------------------


async def _cookie_names(page: Any) -> set[str]:
    """当前浏览器 context 全部 cookie 名（登录态判定用，零 API 请求）。"""
    try:
        cookies = await page.context.cookies()
        return {c.get("name") for c in cookies}
    except Exception:
        return set()


async def _is_logged_in(page: Any, cookie_dict: dict) -> bool:
    """登录态判定：SSOLoginState + WBPSESS 均存在（上游 check_login_state
    同源判定——上游只查这两个 cookie，SUB/SUBP 不用）。cookie_dict 参数
    是构造时快照（可能过期），这里实时读浏览器 context 全部 cookie。
    """
    names = await _cookie_names(page)
    return "SSOLoginState" in names and "WBPSESS" in names


async def _guide_qr_login(page: Any) -> None:
    """在 CDP 页面上打开微博 SSO 登录页，等待用户扫码（最多 120s）。

    登录页 URL 照搬上游 WeiboLogin.weibo_sso_login_url；轮询判定沿用
    上游 check_login_state 的两个 cookie。零 API 请求。
    """
    logger.info("微博未登录 - 在浏览器页面引导扫码登录(最多 %ds)...", _LOGIN_POLL_SECONDS)
    try:
        await page.goto(
            "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog",
            wait_until="domcontentloaded", timeout=15000,
        )
    except Exception as e:
        logger.warning("微博登录页打开失败(不影响轮询): %s", e)

    for _ in range(_LOGIN_POLL_SECONDS):
        await asyncio.sleep(1)
        try:
            names = await _cookie_names(page)
            if "SSOLoginState" in names and "WBPSESS" in names:
                logger.info("微博扫码登录成功")
                return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 客户端调用（#3：client 生命周期在 WeiboPlatform 类上）
# ---------------------------------------------------------------------------


async def _client_call(coro_factory: Callable[[Any], Any]) -> Any:
    """串行化执行 client 调用 + 超时防护；挂起时失效重建 page/client。

    注意 _CALL_TIMEOUT=60（douyin 为 45）：WeiboClient.request 内建
    tenacity 重试（stop=5, wait_fixed=3）——432 等风控响应会走「回首页
    + 刷 cookie + 重试」恢复流程，单次调用合法耗时更长。
    """
    async with WeiboPlatform.mc_lock:
        client = await WeiboPlatform.ensure_client()
        try:
            return await asyncio.wait_for(
                coro_factory(client), timeout=_CALL_TIMEOUT,
            )
        except TimeoutError:
            logger.warning(
                "微博 client 调用超时(%ds),判定 page 挂起,重建", _CALL_TIMEOUT,
            )
            await WeiboPlatform.reset_client()
            raise WeiboClientError(
                "微博请求超时（浏览器页面异常），请重试"
            ) from None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    """从微博 URL 提取微博 ID（mblog.id，数值型）。

    支持：m.weibo.cn/detail/{id}（normalize 生成的规范形态）、
    weibo.com/{uid}/{mid}、weibo.com/status/{mid}、weibo.cn/{mid}。
    注意：详情页 URL 的 {id} 同时接受数值 id 与 base62 mid（页面自动
    重定向），此处只提取 token 不做格式校验。
    """
    for pattern in (_WB_DETAIL_RE, _WB_STATUS_ONLY_RE, _WB_STATUS_RE, _WB_CN_RE):
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def _safe_int(v: Any) -> int:
    """安全整数转换：防御 None / 空串 / 非数字字符串。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _strip_html(text: str) -> str:
    return re.sub(r"<.*?>", "", text or "")


def _parse_publish_time(created_at: Any) -> int:
    """mblog.created_at → epoch 秒（上游同款 rfc2822 格式，北京时间）。

    m.weibo.cn 卡片 created_at 为 RFC2822 形如
    "Mon Aug 14 11:11:11 +0800 2026"（上游 store 层用
    rfc2822_to_timestamp 解析）。防御性兼容日期串/ISO 形态；无法解析
    返回 0（crawler 层对 0 不附 publish_date——缺字段宁可省略，不编造）。
    """
    if not created_at:
        return 0
    s = str(created_at).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _fmt_duration(sec: int) -> str:
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _mblog_is_video(mblog: dict) -> bool:
    """判定 mblog 是否为视频微博（page_info.type == "video" 且可提取流）。"""
    page_info = mblog.get("page_info") or {}
    if not isinstance(page_info, dict):
        return False
    if page_info.get("type") != "video":
        return False
    return bool(
        _extract_video_stream(mblog)
        or (page_info.get("media_info") or {}).get("duration")
    )


def _extract_video_stream(mblog: dict) -> str:
    """从 mblog 提取视频流直链（无水印 mp4）。

    优先级对齐详情页 render_data 实测字段：media_info.stream_url_hd
    （高清）→ stream_url（标清）→ h265_mp4_hd → page_info.urls 的
    mp4_720p_mp4 → mp4_hd_mp4 → mp4_ld_mp4。返回空串表示未找到。
    """
    page_info = mblog.get("page_info") or {}
    if not isinstance(page_info, dict):
        return ""
    media_info = page_info.get("media_info") or {}
    if isinstance(media_info, dict):
        for key in ("stream_url_hd", "stream_url", "h265_mp4_hd", "mp4_720p_mp4"):
            url = media_info.get(key)
            if url and str(url).startswith("http"):
                return str(url)
    urls = page_info.get("urls") or {}
    if isinstance(urls, dict):
        for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4"):
            url = urls.get(key)
            if url and str(url).startswith("http"):
                return str(url)
    return ""


def normalize(item: dict) -> dict:
    """微博 mblog（卡片或详情）→ 统一 schema。"""
    mblog = item.get("mblog", item) or item

    note_id = str(mblog.get("id", "") or "")
    text = _strip_html(mblog.get("text", ""))
    user = mblog.get("user") or {}
    author = user.get("screen_name", "") if isinstance(user, dict) else ""
    page_info = mblog.get("page_info") or {}
    media_info = (page_info.get("media_info") or {}) if isinstance(page_info, dict) else {}
    duration_ms = _safe_int(media_info.get("duration", 0)) if isinstance(media_info, dict) else 0
    duration_sec = duration_ms // 1000 if duration_ms else 0

    # view_count 语义 = 点赞数（微博无公开播放量字段；B13/B14 xhs 先例，
    # capability_notes 已向模型声明）。attitudes_count 为数值。
    view_count = _safe_int(mblog.get("attitudes_count", 0))

    return {
        "video_id": note_id,
        "title": text[:200] if text else "",
        "desc": text[:500] if text else "",
        "publish_time": _parse_publish_time(mblog.get("created_at")),
        "duration": duration_sec,
        "duration_text": _fmt_duration(duration_sec) if duration_sec else "",
        "video_url": f"https://m.weibo.cn/detail/{note_id}" if note_id else "",
        "platform": "weibo",
        "author": author,
        "view_count": view_count,
    }


# ---------------------------------------------------------------------------
# 搜索 + 创作者（httpx 直连 m.weibo.cn，CDP 仅登录态）
# ---------------------------------------------------------------------------

async def _search_via_cdp(keyword: str, limit: int = 10) -> list[dict]:
    """通过 MediaCrawler WeiboClient 搜索视频（SearchType.VIDEO，只取第一页）。

    与 xhs 同款：note_type=VIDEO 服务端只返回视频微博（对齐网页端
    「视频」tab）。空结果不重试（douyin verify_check 教训——空卡片
    重发只会加深风控），返回 [] 优雅降级。
    """
    from ._cdp_browser import check_mediacrawler_available

    if (msg := check_mediacrawler_available()) is not None:
        logger.warning(msg)
        return []
    _import_mediacrawler()
    try:
        resp = await _client_call(
            lambda c: c.get_note_by_keyword(
                keyword=keyword, page=1, search_type=_SearchType.VIDEO,
            )
        )
    except WeiboClientError as e:
        logger.warning("微博搜索失败: %s", e)
        return []

    cards = resp.get("cards", []) if isinstance(resp, dict) else []
    notes = _FilterSearchCard(cards)
    videos = [
        n for n in notes
        if isinstance(n, dict) and _mblog_is_video(n.get("mblog") or {})
    ]
    if not videos:
        # 空结果诊断：转储响应键（静默风控/字段变化的典型信号）
        logger.warning(
            "微博搜索无结果 '%s': resp_keys=%s cards=%d",
            keyword,
            list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__,
            len(cards),
        )
        return []
    results = [normalize(v) for v in videos[:limit]]
    logger.info("微博搜索 '%s': %d 条", keyword, len(results))
    await asyncio.sleep(2)  # 对齐官方 CRAWLER_MAX_SLEEP_SEC=2（搜索后节流）
    return results


# 「找人」搜索接口（2026-08-15 接入）：m.weibo.cn/api/container/getIndex
# containerid=100103type=3&q=昵称。页面内 XHR（同源、withCredentials、
# 浏览器完整 cookie）与真实浏览一致；微博无签名机制，无需 webmssdk 等待。
_USER_SEARCH_XHR_JS = """(name) => new Promise((resolve) => {
  const containerid = '100103type=3&q=' + name;
  const url = 'https://m.weibo.cn/api/container/getIndex?containerid='
    + encodeURIComponent(containerid) + '&page_type=searchall';
  const xhr = new XMLHttpRequest();
  xhr.open('GET', url, true);
  xhr.withCredentials = true;
  xhr.onload = () => resolve({status: xhr.status, body: xhr.responseText});
  xhr.onerror = () => resolve({status: 0, body: ''});
  xhr.send();
})"""


def _parse_creator_uid(creator_id: str) -> str | None:
    """从创作者输入解析纯 uid（URL / 纯数字）。失败返回 None（走昵称搜索）。"""
    s = creator_id.strip()
    if s.isdigit():
        return s
    for pattern in (_WB_PROFILE_RE, _WB_U_RE, _WB_UID_PATH_RE, _WB_CN_UID_RE):
        m = pattern.search(s)
        if m:
            return m.group(1)
    return None


async def _resolve_creator_uid_by_name(name: str) -> str | None:
    """昵称 → uid：CDP 页面内 XHR 调「找人」接口，取第一条（平台相关度排序）。

    勿做精确昵称匹配优先：同名小号会覆盖平台排在前面的知名账号
    （2026-08-14 xhs 张朝阳教训）。
    """
    try:
        page = await WeiboPlatform.get_page(WeiboPlatform._home_url)
    except Exception as e:
        logger.warning("微博用户搜索失败: %s", e)
        return None

    raw = {"status": 0, "body": ""}
    for attempt in range(3):
        try:
            raw = await page.evaluate(_USER_SEARCH_XHR_JS, name)
        except Exception as e:
            logger.warning("微博用户搜索 XHR 异常(第%d次): %s", attempt + 1, e)
            raw = {"status": 0, "body": ""}
        if raw.get("status") == 200 and raw.get("body"):
            break
        await asyncio.sleep(0.5)

    if raw.get("status") != 200 or not raw.get("body"):
        logger.warning("微博用户搜索 XHR 失败: status=%s", raw.get("status"))
        return None
    try:
        data = json.loads(raw["body"])
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("微博用户搜索响应解析失败: %s", e)
        return None
    cards = data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
    if not cards:
        logger.warning("微博用户搜索无结果 '%s': cards=%s", name, cards)
        return None
    # 第一张用户卡（card_type=11）的 card_group 逐项取第一个含 user.id 的条目
    for card in cards:
        if not isinstance(card, dict):
            continue
        for item in card.get("card_group") or []:
            user = (item or {}).get("user") or {}
            uid = user.get("id") if isinstance(user, dict) else None
            if uid:
                logger.info(
                    "微博昵称 '%s' -> 用户 '%s' (uid=%s)",
                    name, user.get("screen_name", ""), uid,
                )
                return str(uid)
    logger.warning("微博用户搜索结果无 uid: cards=%s",
                   json.dumps(cards, ensure_ascii=False)[:200])
    return None


async def _get_creator_via_cdp(creator_id: str, limit: int = 10) -> list[dict]:
    """通过 MediaCrawler WeiboClient 获取创作者视频（支持昵称/主页 URL/UID）。

    只取第一页（每页 10 条，够默认 limit）：不翻页——翻全页会高频请求
    触发风控（douyin 翻页被拦的教训）。顺序：先确保登录（零 API 请求），
    再解析 uid（昵称走页面 XHR），最后一条 get_notes_by_creator。
    """
    from ._cdp_browser import check_mediacrawler_available

    if (msg := check_mediacrawler_available()) is not None:
        logger.warning(msg)
        return []
    _import_mediacrawler()

    # 先构造 client（登录门槛，零 API 请求）——昵称搜索的页面 XHR 也
    # 要求浏览器已登录，未登录时直接进入扫码引导
    try:
        async with WeiboPlatform.mc_lock:
            await WeiboPlatform.ensure_client()
    except Exception as e:
        logger.warning("微博创作者查询失败(CDP): %s", e)
        return []

    uid = _parse_creator_uid(creator_id)
    if uid is None:
        uid = await _resolve_creator_uid_by_name(creator_id)
    if not uid:
        logger.warning("微博创作者解析失败: '%s'", creator_id)
        return []

    try:
        resp = await _client_call(
            lambda c: c.get_notes_by_creator(uid, f"107603{uid}", since_id="")
        )
    except WeiboClientError as e:
        logger.warning("微博创作者查询失败: %s", e)
        return []

    cards = resp.get("cards", []) if isinstance(resp, dict) else []
    notes = [
        n for n in cards
        if isinstance(n, dict) and n.get("card_type") == 9
    ]
    videos = [n for n in notes if _mblog_is_video(n.get("mblog") or {})]
    results = [normalize(v) for v in videos[:limit]]
    logger.info("微博创作者 %s: %d 条(第一页)", uid, len(results))
    await asyncio.sleep(2)  # 对齐官方 CRAWLER_MAX_SLEEP_SEC=2（批量后节流）
    return results


# ---------------------------------------------------------------------------
# 下载（详情页 render_data → 视频流直链 → httpx 下载文件）
# ---------------------------------------------------------------------------

async def _download_via_cdp(video_url: str, file_name: str,
                            progress_callback: Callable[[int], None] | None = None) -> dict:
    """通过 MediaCrawler 获取微博详情（render_data），httpx 下载视频文件。

    详情走上游 get_note_info_by_id（页面 $render_data 解析，MC 官方唯一
    的视频数据源——搜索卡片无流地址）；图文微博返回 fatal（xhs 先例）。
    """
    from ._cdp_browser import check_mediacrawler_available

    if (msg := check_mediacrawler_available()) is not None:
        return {"status": "error", "error": msg, "video_url": video_url}
    from vidagent.utils import storage as _storage

    target = _storage.media_path(file_name, ".mp4")
    if target.exists():
        logger.info("下载命中缓存: %s", target)
        if progress_callback:
            progress_callback(100)
        return {"status": "success", "local_path": str(target), "platform": "weibo", "cached": True}

    logger.info("微博下载开始: url=%s", video_url)
    _import_mediacrawler()

    note_id = extract_video_id(video_url)
    if not note_id:
        logger.error("无法解析微博 ID: url=%s", video_url)
        return {"status": "error", "error": f"无法解析微博视频 ID: {video_url}", "video_url": video_url}

    try:
        detail = await _client_call(lambda c: c.get_note_info_by_id(note_id))
    except WeiboClientError as e:
        return {"status": "error", "error": str(e), "video_url": video_url}

    if not isinstance(detail, dict):
        mblog = {}
    else:
        mblog = detail.get("mblog") or {}
    if not mblog:
        logger.error("微博详情为空(可能已删除或需登录): id=%s", note_id)
        return {"status": "error", "error": "微博不存在或已被删除", "video_url": video_url}

    if not _mblog_is_video(mblog):
        # fatal：图文微博是确定性结果，重试无意义（server 层不再重试）
        return {"status": "error", "fatal": True,
                "error": "该微博为图文微博，暂不支持视频总结", "video_url": video_url}

    media_url = _extract_video_stream(mblog)
    if not media_url:
        page_info = mblog.get("page_info") or {}
        logger.error(
            "微博视频流地址缺失: page_info keys=%s",
            sorted(page_info.keys()) if isinstance(page_info, dict) else type(page_info).__name__,
        )
        return {"status": "error", "error": "未找到可下载的视频链接", "video_url": video_url}
    logger.info("下载链接: %s...", media_url[:80])

    # 流式下载（Referer 模拟正常请求，.part 先写再原子替换）
    part: Path | None = None
    try:
        dl_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://m.weibo.cn/detail/{note_id}",
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
                part = target.with_name(target.name + ".part")
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and progress_callback:
                            pct = int(downloaded / total * 100)
                            progress_callback(pct)
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
    return {"status": "success", "local_path": str(target), "platform": "weibo", "resolved_url": media_url}


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class WeiboPlatform(MediaCrawlerPlatform):
    name: ClassVar[str] = "weibo"
    aliases: ClassVar[tuple[str, ...]] = ("wb", "微博")
    url_patterns: ClassVar[tuple[str, ...]] = ("weibo.com", "weibo.cn", "m.weibo.cn")
    supports_hot: ClassVar[bool] = False
    supports_search: ClassVar[bool] = True
    supports_creator: ClassVar[bool] = True
    # view_count 语义 = 点赞数（微博无公开播放量字段；B13/B14 xhs 先例，
    # 生成器并入工具 describe 平台句与 SYSTEM_PROMPT 知识片段）
    capability_notes: ClassVar[dict[str, str]] = {
        "search": "微博搜索结果 view_count 为点赞数（微博无公开播放量）",
        "creator": "微博创作者视频列表 view_count 为点赞数（微博无公开播放量）",
    }

    # -- MediaCrawlerPlatform 声明（#3） --
    cdp_page_key: ClassVar[str] = "wb"
    mc_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    mc_submodules: ClassVar[tuple[str, ...]] = ("client", "field", "help", "exception")
    mc_package: ClassVar[str] = "weibo"
    _client_cls_name: ClassVar[str] = "WeiboClient"
    _client_timeout: ClassVar[float] = _CLIENT_TIMEOUT
    _home_url: ClassVar[str] = "https://m.weibo.cn"
    # 双域：SSOLoginState 在 .weibo.com、WBPSESS 在 .weibo.cn（登录态判定
    # 需要两者；convert_browser_context_cookies 按 URL 域过滤）
    _cookie_urls: ClassVar[tuple[str, ...]] = ("https://m.weibo.cn", "https://weibo.com")

    # -- 客户端生命周期 hooks（模板见 MediaCrawlerPlatform.ensure_client） --

    @classmethod
    async def _acquire_page(cls) -> Any:
        """CDP 页面取用（含微博错误翻译与失效重建）。"""
        try:
            page = await cls.get_page(cls._home_url)
        except RuntimeError:
            raise WeiboClientError(CDP_GUIDE_MSG) from None
        except Exception as e:
            await invalidate_page(cls.cdp_page_key)
            raise WeiboClientError(f"微博页面加载失败: {e}") from e
        return page

    @classmethod
    async def _build_headers(cls, page: Any, cookie_str: str) -> dict:
        # 请求头完全复刻上游 create_weibo_client（core.py:339-358）——
        # 移动 UA + Cookie + Origin/Referer 是微博风控放行的关键，
        # 缺任何一项都可能被静默拒绝
        return {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Cookie": cookie_str,
            "Origin": "https://m.weibo.cn",
            "Referer": "https://m.weibo.cn",
            "Content-Type": "application/json;charset=UTF-8",
        }

    @classmethod
    async def _is_logged_in(cls, page: Any, cookie_dict: dict) -> bool:
        # 上游 check_login_state 同源判定（实时读浏览器 cookie，
        # 不信任构造时的 cookie_dict 快照——引导登录后模板会用旧快照复查）
        return await _is_logged_in(page, cookie_dict)

    @classmethod
    async def _guide_login(cls, page: Any, client: Any) -> None:
        await _guide_qr_login(page)

    @classmethod
    async def _handle_login_failure(cls, page: Any, client: Any) -> Any:
        """微博登录是硬门槛：未登录 → 清 client 并抛错。

        未登录请求 m.weibo.cn 基本必触发验证码（风控诱因），
        软放行（xhs 模式）不适用。
        """
        cls._client = None
        cls._client_page = None
        raise WeiboLoginError(
            f"微博未登录：扫码登录超时（{_LOGIN_POLL_SECONDS}s），"
            "请先在 Chrome 中登录微博后重试"
        )

    @classmethod
    def _client_proxy(cls) -> str | None:
        """国内平台直连（镜像 xhs/douyin：代理出口 IP 会被微博风控静默拒绝）。"""
        return None

    # -- 检索 / 下载 --

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return extract_video_id(url)

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, proxy=_get_proxy())

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 10) -> list[dict]:
        raise NotImplementedError(
            "微博热搜榜暂未接入（已列入计划），"
            "请改用关键词搜索（search_videos），例如「搜索微博的视频」"
        )

    @classmethod
    async def search(cls, client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索视频（MediaCrawler httpx 直连，提交到 CDP 循环执行）。"""
        return await cls.run_on_cdp_async(_search_via_cdp(keyword, limit))

    @classmethod
    async def get_creator(cls, client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频（MediaCrawler httpx 直连，提交到 CDP 循环执行）。"""
        return await cls.run_on_cdp_async(_get_creator_via_cdp(creator, limit))

    @classmethod
    def download(cls, video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        """下载微博视频（MediaCrawler 详情 → httpx）。

        同步入口：提交到 CDP 专用常驻循环执行，避免 asyncio.run 临时循环
        与模块级 Playwright 单例的跨循环复用问题。
        """
        return cls.run_on_cdp(
            _download_via_cdp(video_url, file_name, progress_callback=progress_callback)
        )


# 注册到全局注册表
register(WeiboPlatform)
