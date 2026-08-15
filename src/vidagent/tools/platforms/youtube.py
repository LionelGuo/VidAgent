"""YouTube 平台适配：Data API v3 检索 + yt-dlp 下载。

API Key 可选：无 key 时搜索降级为 yt-dlp ytsearch（元数据较少）。
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import httpx
import yt_dlp

from vidagent.config import settings
from vidagent.tools.platforms import Platform, register
from vidagent.utils import storage
from vidagent.utils.timer import Timer

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# YouTube video ID: 11 chars alphanumeric + _ -
_YT_ID_RE = re.compile(r"(?:v=|/v/|youtu\.be/|/embed/)([\w\-]{11})")

# ISO 8601 duration: PT1H2M3S → seconds
_ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _get_api_key() -> str:
    """从配置读取 YouTube API key。"""
    return settings.youtube_api_key


def _get_proxy() -> str | None:
    """从配置读取代理地址，用于 httpx。"""
    p = settings.youtube_proxy
    return p if p else None


def _ytdlp_proxy() -> str | None:
    """从配置读取代理地址，用于 yt-dlp。"""
    return settings.youtube_proxy or None


def _ytdlp_cookiefile() -> str | None:
    """从配置读取 cookie 文件路径（Netscape 格式），用于 yt-dlp。"""
    c = settings.youtube_cookie
    if c and not c.startswith("http") and not c.startswith("SESS"):
        # 文件路径
        import os as _os
        if _os.path.isfile(c):
            return c
    return None


async def _enrich(client: httpx.AsyncClient, results: list[dict]) -> None:
    """用一次 videos.list 补全时长/播放量（search 只返回 snippet，无 contentDetails）。

    失败仅告警、不阻断主流程（元数据缺失时前端显示 00:00，检索结果仍可用）。
    """
    ids = [v.get("video_id") for v in results if v.get("video_id")]
    if not ids:
        return
    try:
        data = await _api_get(client, "/videos", {
            "part": "contentDetails,statistics",
            "id": ",".join(ids),
        })
    except RuntimeError as e:
        logger.warning("YouTube 元数据补全失败: %s", e)
        return
    by_id = {it.get("id"): it for it in data.get("items", [])}
    for v in results:
        detail = by_id.get(v.get("video_id")) or {}
        content = detail.get("contentDetails", {})
        stats = detail.get("statistics", {})
        if content.get("duration"):
            v["duration"] = _parse_iso_duration(content["duration"])
            v["duration_text"] = _fmt_duration(v["duration"])
        if stats.get("viewCount"):
            try:
                v["view_count"] = int(stats["viewCount"])
            except (ValueError, TypeError):
                pass


def _parse_iso_duration(raw: str | None) -> int:
    """ISO 8601 duration → 秒。如 PT1H2M3S → 3723。"""
    if not raw:
        return 0
    m = _ISO_DURATION_RE.match(raw)
    if not m:
        return 0
    h, mins, secs = m.groups()
    return int(h or 0) * 3600 + int(mins or 0) * 60 + int(secs or 0)


def _fmt_duration(sec: int) -> str:
    """秒 → 'MM:SS' 或 'H:MM:SS'。"""
    if sec <= 0:
        return "00:00"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_published_at(raw: str | None) -> int:
    """ISO 8601 datetime → unix timestamp。"""
    if not raw:
        return 0
    try:
        # Python 3.11+: datetime.fromisoformat handles Z suffix
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    """从 YouTube URL 提取 video ID。"""
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def make_client(api_key: str | None = None, timeout: float = 15.0) -> httpx.AsyncClient:
    """创建 YouTube API HTTP 客户端（自动使用代理）。"""
    proxy = _get_proxy()
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers=DEFAULT_HEADERS,
        timeout=timeout,
        params={"key": api_key or _get_api_key()},
        proxy=proxy,
    )


def normalize(item: dict) -> dict:
    """YouTube API 或 yt-dlp 结果 → 统一 schema。"""
    # ── API v3 格式 ──
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    content_details = item.get("contentDetails", {})

    # video ID：search 结果嵌套在 id.videoId，videos 结果在顶层 id
    if isinstance(item.get("id"), dict):
        video_id = item["id"].get("videoId", "")
    else:
        video_id = item.get("id", "")

    title = snippet.get("title", "") or item.get("title", "")
    desc = snippet.get("description", "") or item.get("description", "") or ""
    author = snippet.get("channelTitle", "") or item.get("channel", "") or item.get("uploader", "") or ""

    # 发布时间
    published_at = snippet.get("publishedAt") or item.get("upload_date") or None
    if published_at and isinstance(published_at, str) and len(published_at) == 8:
        # yt-dlp 格式: YYYYMMDD
        try:
            dt = datetime.strptime(published_at, "%Y%m%d")
            publish_time = int(dt.replace(tzinfo=UTC).timestamp())
        except ValueError:
            publish_time = _parse_published_at(published_at)
    else:
        publish_time = _parse_published_at(published_at)

    # 时长
    duration_raw = content_details.get("duration") or item.get("duration_string") or item.get("duration")
    if isinstance(duration_raw, (int, float)):
        duration = int(duration_raw)
    elif isinstance(duration_raw, str) and duration_raw.startswith("PT"):
        duration = _parse_iso_duration(duration_raw)
    elif isinstance(duration_raw, str) and ":" in duration_raw:
        # mm:ss or hh:mm:ss string
        parts = duration_raw.split(":")
        try:
            nums = [int(p) for p in parts]
            if len(nums) == 2:
                duration = nums[0] * 60 + nums[1]
            elif len(nums) == 3:
                duration = nums[0] * 3600 + nums[1] * 60 + nums[2]
            else:
                duration = 0
        except ValueError:
            duration = 0
    else:
        duration = int(duration_raw) if duration_raw else 0

    # 播放量
    view_count_str = statistics.get("viewCount") or item.get("view_count")
    try:
        view_count = int(view_count_str) if view_count_str else 0
    except (ValueError, TypeError):
        view_count = 0

    # URL
    video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else item.get("webpage_url", "")

    return {
        "video_id": video_id,
        "title": title,
        "desc": desc[:500] if desc else "",  # 截断长简介
        "publish_time": publish_time,
        "duration": duration,
        "duration_text": _fmt_duration(duration),
        "video_url": video_url,
        "platform": "youtube",
        "author": author,
        "view_count": view_count,
    }


# ---------------------------------------------------------------------------
# 检索（YouTube Data API v3）
# ---------------------------------------------------------------------------

async def _api_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    """YouTube API GET 请求，处理配额耗尽等错误。"""
    p = dict(params or {})
    # API key 已通过 make_client 的 default params 注入
    try:
        resp = await client.get(path, params=p)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            msg = err.get("message", "未知错误")
            if code == 403 and "quota" in msg.lower():
                raise RuntimeError(f"YouTube API 配额已耗尽: {msg}")
            raise RuntimeError(f"YouTube API 错误 ({code}): {msg}")
        return data
    except httpx.HTTPError as e:
        raise RuntimeError(f"YouTube API 网络错误: {e}") from e


# ---------------------------------------------------------------------------
# yt-dlp 降级搜索（无 API key 时）
# ---------------------------------------------------------------------------

def _ytdlp_search(keyword: str, limit: int = 10) -> list[dict]:
    """用 yt-dlp 内置 ytsearch 搜索（同步，在线程池中调用）。"""
    try:
        opts = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "extract_flat": True,
            "force_generic_extractor": False,
        }
        _apply_ytdlp_opts(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max(limit, 5)}:{keyword}", download=False)
            entries = info.get("entries", []) if info else []
            return [normalize(e) for e in entries[:limit] if e]
    except Exception as e:
        logger.warning("yt-dlp 搜索降级失败: %s", e)
        return []


def _ytdlp_trending(limit: int = 10) -> list[dict]:
    """用 yt-dlp 获取 YouTube trending（同步）。"""
    try:
        opts = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "extract_flat": True,
        }
        _apply_ytdlp_opts(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/feed/trending", download=False)
            entries = info.get("entries", []) if info else []
            return [normalize(e) for e in entries[:limit] if e]
    except Exception as e:
        logger.warning("yt-dlp trending 失败: %s", e)
        return []


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _apply_ytdlp_opts(opts: dict) -> None:
    """将代理、cookie、JS runtime 等通用配置注入 yt-dlp opts。"""
    proxy = _ytdlp_proxy()
    if proxy:
        opts["proxy"] = proxy
    cookiefile = _ytdlp_cookiefile()
    if cookiefile:
        # 仅在文件存在且非空时使用（过期 cookie 可能导致格式列表异常）
        try:
            import os as _os
            if _os.path.getsize(cookiefile) > 100:
                opts["cookiefile"] = cookiefile
            else:
                logger.info("Cookie 文件过小,跳过: %s", cookiefile)
        except OSError:
            pass

    # yt-dlp 2026+：YouTube 签名/挑战求解需要 JS runtime（默认仅启用 deno）。
    # 本机 node >= 22 即可；无 node 时保持 yt-dlp 默认（格式受限，但可降级下载）。
    # 挑战求解脚本首次使用会远程拉取一次（走 proxy，缓存于 ~/.cache/yt-dlp）。
    node_path = shutil.which("node")
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}
        opts["remote_components"] = ["ejs:github"]


def _download_yt(url: str, file_name: str,
                  progress_callback: Callable[[int], None] | None = None) -> dict:
    """yt-dlp 下载 YouTube 视频。

    先尝试带 cookie（如有配置）；若因格式不可用失败则自动回退到无 cookie 模式。
    """
    target = storage.media_path(file_name, ".mp4")

    def _try_download(extra_opts: dict | None = None) -> bool:
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
            # 1080p 封顶：JS runtime 解锁后 bestvideo 会选到 4K/8K，
            # 长视频动辄数 GB，而总结抽帧只需 512×512，超清纯属浪费
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "progress_hooks": progress_hooks,
        }
        _apply_ytdlp_opts(opts)
        if extra_opts:
            opts.update(extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return True

    # 降级链：默认客户端（最高画质）→ web_embedded 客户端（绕过 GVS PO Token
    # 实验，YouTube 2026 起部分视频〔如影视预告片〕的高清流 403）→ 无 cookie
    attempts: list[dict] = [
        {},
        {"extractor_args": {"youtube": {"player_client": ["web_embedded", "mweb"]}}},
    ]
    if _ytdlp_cookiefile():
        attempts.append({"cookiefile": None})

    last_error = None
    for extra in attempts:
        try:
            label = "YouTube下载(yt-dlp)"
            if extra:
                label += " | " + ("no-cookie" if "cookiefile" in extra else "embedded-client")
            with Timer(label):
                _try_download(extra)
            last_error = None
            break
        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
            logger.warning("YouTube 下载失败(将尝试下一级降级): %s", str(e)[:200])

    if last_error:
        return {"status": "error", "error": f"yt-dlp 下载失败: {last_error}", "video_url": url}

    local = _find_result(storage.sanitize(file_name))
    if not local:
        return {"status": "error", "error": "下载完成但未找到产物文件", "video_url": url}
    return {"status": "success", "local_path": str(local), "platform": "youtube"}


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

class YoutubePlatform(Platform):
    name: ClassVar[str] = "youtube"
    aliases: ClassVar[tuple[str, ...]] = ("yt", "ytb", "油管")
    url_patterns: ClassVar[tuple[str, ...]] = ("youtube.com", "youtu.be")
    supports_hot: ClassVar[bool] = True
    supports_search: ClassVar[bool] = True
    supports_creator: ClassVar[bool] = True
    capability_notes: ClassVar[dict[str, str]] = {
        "creator": "YouTube 创作者查询需后端配置 API key",
    }

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        return extract_video_id(url)

    @staticmethod
    def normalize(raw: dict) -> dict:
        return normalize(raw)

    @staticmethod
    def make_client(api_key: str | None = None, timeout: float = 15.0) -> httpx.AsyncClient:
        return make_client(api_key, timeout)

    @staticmethod
    async def search(client: httpx.AsyncClient, keyword: str, limit: int = 10) -> list[dict]:
        api_key = _get_api_key()
        if not api_key:
            # 降级：yt-dlp ytsearch（同步 → 线程池）
            import asyncio
            loop = asyncio.get_event_loop()
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor() as pool:
                return await loop.run_in_executor(pool, _ytdlp_search, keyword, limit)

        data = await _api_get(client, "/search", {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "maxResults": min(limit, 50),
        })
        items = data.get("items", [])
        results = [normalize(it) for it in items[:limit]]
        await _enrich(client, results)
        return results

    @staticmethod
    async def get_hot(client: httpx.AsyncClient, limit: int = 10) -> list[dict]:
        api_key = _get_api_key()
        if not api_key:
            import asyncio
            loop = asyncio.get_event_loop()
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor() as pool:
                return await loop.run_in_executor(pool, _ytdlp_trending, limit)

        # YouTube trending = videos.list chart=mostPopular
        data = await _api_get(client, "/videos", {
            "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular",
            "regionCode": "US",
            "maxResults": min(limit, 50),
        })
        items = data.get("items", [])
        return [normalize(it) for it in items[:limit]]

    @staticmethod
    async def get_creator(client: httpx.AsyncClient, creator: str, limit: int = 10) -> list[dict]:
        """获取 YouTube 频道视频。

        creator 可以是频道 ID（UC...）或 @handle。
        优先按频道 ID 处理，否则搜索频道。
        """
        api_key = _get_api_key()
        if not api_key:
            raise NotImplementedError("YouTube 创作者查询需要 API key（yt-dlp 不支持频道检索）")

        channel_id = creator
        # 如果看起来不是频道 ID（不以 UC 开头），尝试解析
        if not creator.startswith("UC"):
            # 尝试按 handle 搜索频道
            # YouTube API: search for channel by handle
            data = await _api_get(client, "/search", {
                "part": "snippet",
                "q": creator,
                "type": "channel",
                "maxResults": 1,
            })
            items = data.get("items", [])
            if not items:
                raise ValueError(f"找不到 YouTube 频道: {creator}")
            channel_id = items[0]["snippet"]["channelId"]
            channel_title = items[0]["snippet"]["channelTitle"]
            logger.info("YouTube 频道'%s'解析为 %s (%s)", creator, channel_id, channel_title)

        # 按频道 ID 搜索视频
        data = await _api_get(client, "/search", {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": min(limit, 50),
        })
        items = data.get("items", [])
        results = [normalize(it) for it in items[:limit]]
        # 与 search 同款富化（B15 修复：曾漏调导致时长/播放量全为 0——
        # search.list 只返回 snippet，无 contentDetails/statistics）
        await _enrich(client, results)
        return results

    @staticmethod
    def download(video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        return _download_yt(video_url, file_name, progress_callback=progress_callback)


# 注册到全局注册表
register(YoutubePlatform)
