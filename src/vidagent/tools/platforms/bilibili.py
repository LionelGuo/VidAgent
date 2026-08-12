"""B站平台适配：REST API 封装 + WBI 签名 + yt-dlp 下载。

公开数据接口免登录；search 与创作者主页需 WBI 签名（见 vidagent.utils.wbi）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable, ClassVar

import httpx
import yt_dlp

from vidagent.tools.platforms import Platform, register
from vidagent.utils import storage
from vidagent.utils.timer import Timer
from vidagent.utils.wbi import get_wbi_keys, sign_wbi

logger = logging.getLogger(__name__)

API_BASE = "https://api.bilibili.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_BV_RE = re.compile(r"(BV[\w]+)")


def _parse_cookies(cookie_str: str) -> dict:
    """把 'k1=v1; k2=v2' 解析为 dict。"""
    out: dict[str, str] = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out


def make_client(cookie: str | None = None, timeout: float = 15.0) -> httpx.AsyncClient:
    client = httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout)
    if cookie:
        for k, v in _parse_cookies(cookie).items():
            client.cookies.set(k, v, domain=".bilibili.com")
    return client


async def _ensure_fingerprint(client: httpx.AsyncClient) -> None:
    """注入 buvid3/buvid4 指纹（部分接口降低风控触发）。已存在则跳过。"""
    if "buvid3" in client.cookies:
        return
    try:
        spi = (await client.get(f"{API_BASE}/x/frontend/finger/spi")).json()
        b3 = spi.get("data", {}).get("b_3")
        b4 = spi.get("data", {}).get("b_4")
        if b3:
            client.cookies.set("buvid3", b3, domain=".bilibili.com")
        if b4:
            client.cookies.set("buvid4", b4, domain=".bilibili.com")
    except Exception:  # 指纹非必需，失败则忽略
        pass


class BiliAPIError(RuntimeError):
    """B站接口错误（网络失败 / 非 JSON / 业务 code != 0）。"""


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """带重试的 GET + JSON 解析：网络错误与非 JSON（如 412 风控页）最多重试 3 次。"""
    last: object = None
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params)
            try:
                return resp.json()
            except ValueError:
                last = BiliAPIError(f"非 JSON 响应(HTTP {resp.status_code})，疑似风控拦截")
        except httpx.HTTPError as e:  # 连接/超时等瞬时错误
            last = e
        if attempt < 2:
            await asyncio.sleep(1.0 * (attempt + 1))
    raise BiliAPIError(f"B站接口请求失败({url}): {last}")


def _check(data: dict, where: str) -> dict:
    """校验业务返回 code==0，否则抛 BiliAPIError。"""
    if data.get("code") != 0:
        raise BiliAPIError(
            f"B站接口 {where} 返回错误 code={data.get('code')} msg={data.get('message')}"
        )
    return data


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _author_name(item: dict) -> str:
    author = item.get("author")
    if isinstance(author, str):
        return author
    owner = item.get("owner")
    if isinstance(owner, dict):
        return owner.get("name", "")
    return ""


def _view_count(item: dict) -> int:
    stat = item.get("stat")
    if isinstance(stat, dict) and stat.get("view") is not None:
        return int(stat["view"])
    play = item.get("play")
    return int(play) if play is not None else 0


def _parse_duration(raw) -> int:
    """时长 → 秒。兼容 int 秒、'M:SS'、'H:MM:SS'；无法解析返回 0。"""
    if raw is None or raw == "":
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    parts = str(raw).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:  # M:SS
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:  # H:MM:SS
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _fmt_duration(sec: int) -> str:
    """秒 → 'MM:SS' 或 'H:MM:SS'。"""
    if sec <= 0:
        return "00:00"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def normalize(item: dict) -> dict:
    """将 B站各类接口的视频项归一化为统一 schema（含时长）。"""
    bvid = item.get("bvid") or ""
    duration = _parse_duration(item.get("duration") or item.get("length"))
    return {
        "video_id": bvid,
        "title": _strip_html(item.get("title") or ""),
        "desc": _strip_html(item.get("desc") or item.get("description") or ""),
        "publish_time": int(item.get("pubdate") or item.get("created") or 0),
        "duration": duration,  # 秒
        "duration_text": _fmt_duration(duration),
        "video_url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "platform": "bilibili",
        "author": _author_name(item),
        "view_count": _view_count(item),
    }


def extract_video_id(url: str) -> str | None:
    """从 B站 URL 提取 BV 号。"""
    m = _BV_RE.search(url)
    return m.group(1) if m else None


async def fetch_popular(client: httpx.AsyncClient, ps: int = 20, pn: int = 1) -> list[dict]:
    """综合热门（首页 trending，最贴近「今日热榜」）。"""
    data = await _get(client, f"{API_BASE}/x/web-interface/popular", {"ps": ps, "pn": pn})
    _check(data, "popular")
    return [normalize(v) for v in data.get("data", {}).get("list", [])]


async def fetch_ranking(
    client: httpx.AsyncClient, rid: int = 0, type_: str = "all"
) -> list[dict]:
    """排行榜（全站：rid=0）。"""
    data = await _get(
        client, f"{API_BASE}/x/web-interface/ranking/v2", {"rid": rid, "type": type_}
    )
    _check(data, "ranking")
    return [normalize(v) for v in data.get("data", {}).get("list", [])]


async def search_videos(
    client: httpx.AsyncClient, keyword: str, page: int = 1, page_size: int = 20
) -> list[dict]:
    """关键词搜索视频（需 WBI 签名）。"""
    await _ensure_fingerprint(client)
    img_key, sub_key = await get_wbi_keys(client)
    params = sign_wbi(
        {"search_type": "video", "keyword": keyword, "page": page, "page_size": page_size},
        img_key,
        sub_key,
    )
    data = await _get(client, f"{API_BASE}/x/web-interface/wbi/search/type", params)
    _check(data, "search")
    result = data.get("data", {}).get("result", []) or []
    return [normalize(v) for v in result if v.get("type") == "video"]


async def fetch_user_videos(
    client: httpx.AsyncClient, mid: str, pn: int = 1, ps: int = 30, order: str = "pubdate"
) -> list[dict]:
    """创作者主页视频（需 WBI 签名 + 风控 Cookie）。

    注意：该接口风控较严，headless 调用常返回 -352。需在 make_client 时传入
    含 SESSDATA 的登录 Cookie（见 config.BILI_COOKIE）。
    """
    await _ensure_fingerprint(client)
    img_key, sub_key = await get_wbi_keys(client)
    params = sign_wbi({"mid": mid, "pn": pn, "ps": ps, "order": order}, img_key, sub_key)
    data = await _get(client, f"{API_BASE}/x/space/wbi/arc/search", params)
    if data.get("code") != 0:
        raise BiliAPIError(
            f"B站创作者接口失败 code={data.get('code')} msg={data.get('message')}。"
            "通常需在 .env 设置 BILI_COOKIE（含 SESSDATA）。"
        )
    vlist = (data.get("data", {}).get("list", {}) or {}).get("vlist", []) or []
    return [normalize(v) for v in vlist]


def _normalize_user(item: dict) -> dict:
    """归一化「用户」搜索项（区别于视频 normalize）。"""
    return {
        "mid": str(item.get("mid", "")),
        "uname": item.get("uname", ""),
        "fans": int(item.get("fans", 0) or 0),
        "level": int(item.get("level", 0) or 0),
        "face": item.get("face", ""),
    }


async def search_users(
    client: httpx.AsyncClient, keyword: str, page: int = 1, page_size: int = 20
) -> list[dict]:
    """用户搜索（search_type=bili_user，WBI 签名，与视频搜索同族、headless 可用）。

    返回 [{mid, uname, fans, level, face}, ...]。
    """
    await _ensure_fingerprint(client)
    img_key, sub_key = await get_wbi_keys(client)
    params = sign_wbi(
        {"search_type": "bili_user", "keyword": keyword, "page": page, "page_size": page_size},
        img_key,
        sub_key,
    )
    data = await _get(client, f"{API_BASE}/x/web-interface/wbi/search/type", params)
    _check(data, "user_search")
    result = data.get("data", {}).get("result", []) or []
    return [_normalize_user(u) for u in result]


def _pick_best_user(users: list[dict], name: str) -> dict | None:
    """从候选用户中选最佳：优先 uname 完全等于 name（取其中 fans 最多）；
    无精确匹配则取整体 fans 最多的（B站按相关度排序，首位通常即目标）。"""
    if not users:
        return None
    exact = [u for u in users if u.get("uname") == name]
    pool = exact if exact else users
    return max(pool, key=lambda u: u.get("fans", 0))


async def resolve_creator_mid(client: httpx.AsyncClient, name: str) -> tuple[str, str, int]:
    """昵称 → (mid, uname, fans)。搜不到抛 ValueError。"""
    users = await search_users(client, name)
    best = _pick_best_user(users, name)
    if not best:
        raise ValueError(f"找不到 B站创作者「{name}」")
    return best["mid"], best["uname"], best["fans"]


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _download_bili(url: str, file_name: str,
                   progress_callback: Callable[[int], None] | None = None) -> dict:
    storage.random_delay()  # 随机抖动降风控（文档 §5.1）
    target = storage.media_path(file_name, ".mp4")  # 仅用于确定命名前缀
    progress_hooks = []
    if progress_callback:

        def _on_progress(d: dict) -> None:
            if d.get("status") == "finished":
                progress_callback(100)
            elif d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total and total > 0:
                    pct = int(d.get("downloaded_bytes", 0) / total * 100)
                    progress_callback(pct)

        progress_hooks.append(_on_progress)
    opts = {
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "format": "bestvideo*+bestaudio/best",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "progress_hooks": progress_hooks,
    }
    try:
        with Timer("视频下载(yt-dlp)"):
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        return {"status": "error", "error": f"yt-dlp 下载失败: {e}", "video_url": url}

    local = _find_result(storage.sanitize(file_name))
    if not local:
        return {"status": "error", "error": "下载完成但未找到产物文件", "video_url": url}
    return {"status": "success", "local_path": str(local), "platform": "bilibili"}


def _find_result(base_name: str) -> Path | None:
    ws = storage.workspace()
    cand = ws / f"{base_name}.mp4"
    if cand.exists():
        return cand
    mp4s = sorted(ws.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[0] if mp4s else None


# ---------------------------------------------------------------------------
# Platform 实例
# ---------------------------------------------------------------------------

class BilibiliPlatform(Platform):
    name: ClassVar[str] = "bilibili"
    aliases: ClassVar[tuple[str, ...]] = ("bili", "b站")

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return _BV_RE.search(url).group(1) if _BV_RE.search(url) else None

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(cookie: str | None = None, timeout: float = 15.0) -> httpx.AsyncClient:
        return make_client(cookie, timeout)

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        items = await search_videos(client, keyword, page_size=max(limit, 20))
        return items[:limit]

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 10) -> list[dict]:
        items = await fetch_popular(client, ps=max(limit, 20))
        return items[:limit]

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        mid = str(creator)
        if not mid.isdigit():
            mid, uname, fans = await resolve_creator_mid(client, mid)
            logger.info("创作者「%s」解析为 mid=%s（%s，粉丝 %s）", creator, mid, uname, fans)
        items = await fetch_user_videos(client, mid, ps=max(limit, 30))
        return items[:limit]

    @staticmethod
    def download(video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        return _download_bili(video_url, file_name, progress_callback=progress_callback)


# 注册到全局注册表
register(BilibiliPlatform)
