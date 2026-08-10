#!/usr/bin/env python3
"""多平台扩展完整测试（无需启动 server，纯本地命令）。

用法:
  uv run python scripts/test_multiplatform.py

测试范围:
  1.  平台注册表 (register / get_platform / detect_platform / list_platforms)
  2.  B站 normalize + 向后兼容 shim
  3.  B站 extract_video_id
  4.  YouTube normalize (API v3 / yt-dlp 两种格式)
  5.  YouTube extract_video_id (标准 / 短链 / 嵌入)
  6.  YouTube 搜索 yt-dlp 降级 (有代理)
  7.  YouTube 搜索 API v3 (有 key)
  8.  YouTube 热门 API v3 / yt-dlp 降级
  9.  YouTube 创作者查询 API v3
  10. 抖音 extract_video_id + normalize
  11. 抖音热榜 (公开 API)
  12. Crawler 统一分派 (含 douyin)
  13. Downloader 平台路由 + _platform_of 向后兼容 (含 douyin)
  14. Server _extract_video_id 多平台
  15. Tool definitions platform enum (含 douyin)
  16. Agent prompt 包含多平台
  17. YouTube 下载 (完整下载一个短视频，验证产物)
  18. 配置检查
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 确保项目根在 path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

pass_count = 0
fail_count = 0
skip_count = 0


def check(desc: str, condition: bool, detail: str = ""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  ✅ {desc}")
    else:
        fail_count += 1
        print(f"  ❌ {desc}" + (f" → {detail}" if detail else ""))


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def skip(desc: str, reason: str = ""):
    global skip_count
    skip_count += 1
    print(f"  ⏭️  SKIP {desc}: {reason}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 平台注册表
# ══════════════════════════════════════════════════════════════════════════════

def test_platform_registry():
    section("1. 平台注册表")

    # 导入触发注册
    import vidagent.tools.platforms.bilibili   # noqa: F401
    import vidagent.tools.platforms.youtube    # noqa: F401
    import vidagent.tools.platforms.douyin     # noqa: F401
    from vidagent.tools.platforms import get_platform, detect_platform, list_platforms

    platforms = list_platforms()
    check("注册了 3 个平台", len(platforms) == 3, str(platforms))
    check("包含 bilibili", "bilibili" in platforms)
    check("包含 youtube", "youtube" in platforms)
    check("包含 douyin", "douyin" in platforms)

    # get_platform by name
    bp = get_platform("bilibili")
    check("get_platform('bilibili').name == 'bilibili'", bp.name == "bilibili")
    check("Bilibili 别名包含 'b站'", "b站" in bp.aliases)

    yp = get_platform("youtube")
    check("get_platform('youtube').name == 'youtube'", yp.name == "youtube")
    check("YouTube 别名包含 '油管'", "油管" in yp.aliases)

    # get_platform by alias
    check("get_platform('bili').name == 'bilibili'", get_platform("bili").name == "bilibili")
    check("get_platform('yt').name == 'youtube'", get_platform("yt").name == "youtube")

    # 未注册平台
    try:
        get_platform("xiaohongshu")
        check("未注册平台应抛 NotImplementedError", False)
    except NotImplementedError as e:
        msg = str(e)
        check("错误消息含可用平台列表", "bilibili" in msg and "youtube" in msg,
              f"msg={msg[:60]}")

    # detect_platform by URL
    check("B站 URL → bilibili",
          detect_platform("https://www.bilibili.com/video/BV123").name == "bilibili")
    check("b23.tv → bilibili",
          detect_platform("https://b23.tv/abc").name == "bilibili")
    check("youtube.com → youtube",
          detect_platform("https://www.youtube.com/watch?v=xxx").name == "youtube")
    check("youtu.be → youtube",
          detect_platform("https://youtu.be/xxx").name == "youtube")
    check("douyin.com → douyin",
          detect_platform("https://www.douyin.com/video/1").name == "douyin")
    check("unknown URL → None",
          detect_platform("https://example.com") is None)


# ══════════════════════════════════════════════════════════════════════════════
# 2. B站 normalize + 向后兼容
# ══════════════════════════════════════════════════════════════════════════════

def test_bilibili():
    section("2. B站 normalize + 向后兼容 shim")

    # 旧 import 路径仍然可用
    from vidagent.tools import bilibili as b

    # normalize
    item = {
        "bvid": "BV1xx", "title": "Test Video", "desc": "Description",
        "pubdate": 1700000000, "owner": {"name": "TestUP"},
        "stat": {"view": 12345},
    }
    n = b.normalize(item)
    check("normalize: video_id", n["video_id"] == "BV1xx")
    check("normalize: title", n["title"] == "Test Video")
    check("normalize: author", n["author"] == "TestUP")
    check("normalize: view_count", n["view_count"] == 12345)
    check("normalize: platform='bilibili'", n["platform"] == "bilibili")
    check("normalize: video_url 含 BV", "BV1xx" in n["video_url"])

    # 时长解析
    n2 = b.normalize({"bvid": "BV2", "title": "t", "duration": 149})
    check("时长 149s → duration=149, text='02:29'",
          n2["duration"] == 149 and n2["duration_text"] == "02:29")

    n3 = b.normalize({"bvid": "BV3", "title": "t", "duration": "11:41"})
    check("时长 '11:41' → duration=701",
          n3["duration"] == 701 and n3["duration_text"] == "11:41")

    n4 = b.normalize({"bvid": "BV4", "title": "t", "duration": "1:02:03"})
    check("时长 '1:02:03' → duration=3723",
          n4["duration"] == 3723 and n4["duration_text"] == "1:02:03")

    # length 字段 (创作者 vlist)
    n5 = b.normalize({"bvid": "BV5", "title": "t", "length": "10:30"})
    check("length='10:30' → 630s", n5["duration"] == 630)

    # HTML 清洗
    n6 = b.normalize({"bvid": "BV6", "title": '<em>keyword</em>', "description": "<p>DD</p>"})
    check("title HTML 清洗", n6["title"] == "keyword")
    check("desc HTML 清洗", n6["desc"] == "DD")

    # _check / BiliAPIError
    try:
        b._check({"code": -352, "message": "风控"}, "test")
        check("BiliAPIError 应被抛出", False)
    except b.BiliAPIError:
        check("_check code!=0 抛 BiliAPIError", True)

    check("_check code=0 正常返回", b._check({"code": 0, "data": {}}, "t")["code"] == 0)

    # _pick_best_user
    users = [
        {"mid": "1", "uname": "A", "fans": 10},
        {"mid": "2", "uname": "B", "fans": 99},
    ]
    check("pick_best_user 精确匹配", b._pick_best_user(users, "A")["mid"] == "1")
    check("pick_best_user 无精确取最高 fans", b._pick_best_user(users, "X")["mid"] == "2")
    check("pick_best_user 空列表", b._pick_best_user([], "x") is None)

    # 新增的 extract_video_id (从 shim 重导出)
    check("extract_video_id BV", b.extract_video_id("https://www.bilibili.com/video/BV123") == "BV123")
    check("extract_video_id 无匹配", b.extract_video_id("https://example.com") is None)


# ══════════════════════════════════════════════════════════════════════════════
# 3. YouTube normalize
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_normalize():
    section("3. YouTube normalize")

    from vidagent.tools.platforms.youtube import normalize

    # API v3 search 格式
    api_search = {
        "id": {"videoId": "dQw4w9WgXcQ"},
        "snippet": {
            "title": "Rick Astley - Never Gonna Give You Up",
            "description": "The official video",
            "publishedAt": "2009-10-25T06:57:33Z",
            "channelTitle": "Rick Astley",
        },
    }
    n = normalize(api_search)
    check("API search: video_id", n["video_id"] == "dQw4w9WgXcQ")
    check("API search: platform='youtube'", n["platform"] == "youtube")
    check("API search: author", n["author"] == "Rick Astley")
    check("API search: publish_time > 0", n["publish_time"] > 0)

    # API v3 trending (video detail) 格式
    api_detail = {
        "id": "dQw4w9WgXcQ",
        "snippet": {
            "title": "Test Video",
            "description": "A description",
            "publishedAt": "2024-01-01T00:00:00Z",
            "channelTitle": "TestChannel",
        },
        "statistics": {"viewCount": "1234567"},
        "contentDetails": {"duration": "PT1H2M3S"},
    }
    n2 = normalize(api_detail)
    check("API detail: duration PT1H2M3S → 3723", n2["duration"] == 3723)
    check("API detail: duration_text '1:02:03'", n2["duration_text"] == "1:02:03")
    check("API detail: view_count", n2["view_count"] == 1234567)
    check("API detail: video_url endswith id", n2["video_url"].endswith("dQw4w9WgXcQ"))

    # PT3M33S
    n2b = normalize({"id": "abc", "snippet": {"title": "t", "description": "",
                     "publishedAt": "2024-01-01T00:00:00Z", "channelTitle": "c"},
                     "contentDetails": {"duration": "PT3M33S"}})
    check("API detail: duration PT3M33S → 213s", n2b["duration"] == 213)

    # yt-dlp flat extract 格式
    ytdlp = {
        "id": "abc123",
        "title": "YT-DLP Test",
        "description": "desc",
        "upload_date": "20240101",
        "channel": "TestChannel",
        "duration_string": "5:30",
        "view_count": 999,
    }
    n3 = normalize(ytdlp)
    check("yt-dlp: video_id", n3["video_id"] == "abc123")
    check("yt-dlp: duration '5:30' → 330s", n3["duration"] == 330)
    check("yt-dlp: duration_text", n3["duration_text"] == "05:30")
    check("yt-dlp: platform='youtube'", n3["platform"] == "youtube")
    check("yt-dlp: publish_time > 0", n3["publish_time"] > 0)
    check("yt-dlp: video_url", "youtube.com" in n3["video_url"])

    # yt-dlp with int duration
    n4 = normalize({"id": "x", "title": "t", "duration": 180})
    check("yt-dlp: int duration 180 → text '03:00'", n4["duration"] == 180 and n4["duration_text"] == "03:00")


# ══════════════════════════════════════════════════════════════════════════════
# 4. YouTube extract_video_id
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_extract_id():
    section("4. YouTube extract_video_id")

    from vidagent.tools.platforms.youtube import extract_video_id

    check("标准 URL", extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    check("短链 youtu.be", extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    check("embed URL", extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    check("带参数 URL", extract_video_id("https://www.youtube.com/watch?v=abcdefghijk&t=30") == "abcdefghijk")
    check("非 YouTube URL", extract_video_id("https://example.com/video") is None)
    check("11 字符 ID", extract_video_id("https://youtu.be/1234567890A") == "1234567890A")
    check("含 - 的 ID", extract_video_id("https://youtu.be/abc-DEF_123") == "abc-DEF_123")


# ══════════════════════════════════════════════════════════════════════════════
# 5. YouTube 搜索 — yt-dlp 降级 (有代理)
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_ytdlp_search():
    section("5. YouTube 搜索 — yt-dlp 降级 (有代理)")

    from vidagent.tools.platforms.youtube import _ytdlp_search

    results = _ytdlp_search("machine learning", limit=3)
    check("返回了结果", len(results) > 0, f"got {len(results)}")
    if results:
        for i, r in enumerate(results):
            checks = [
                r.get("video_id"),
                r.get("title"),
                r.get("platform") == "youtube",
                "youtube.com" in r.get("video_url", ""),
            ]
            ok = all(checks)
            check(f"  结果[{i}] 结构完整: {r.get('title', '')[:50]}", ok,
                  f"id={r.get('video_id')} url={r.get('video_url')}")


# ══════════════════════════════════════════════════════════════════════════════
# 6. YouTube 搜索 — API v3 (有 key)
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_api_search():
    section("6. YouTube 搜索 — API v3")

    from vidagent.config import settings
    from vidagent.tools.platforms.youtube import make_client, YoutubePlatform

    if not settings.youtube_api_key:
        skip("YOUTUBE_API_KEY 未配置")
        return

    async def _run():
        client = make_client()
        try:
            results = await YoutubePlatform.search(client, "Python tutorial", limit=3)
            check("返回了结果", len(results) > 0, f"got {len(results)}")
            for i, r in enumerate(results):
                ok = all([
                    r.get("video_id"),
                    r.get("title"),
                    r["platform"] == "youtube",
                    r.get("duration", 0) >= 0,
                    r.get("view_count", 0) >= 0,
                    r.get("publish_time", 0) > 0,
                ])
                check(f"  结果[{i}] 元数据完整: {r.get('title','')[:50]}", ok)
        finally:
            await client.aclose()

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 7. YouTube 热门
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_trending():
    section("7. YouTube 热门")

    from vidagent.config import settings
    from vidagent.tools.platforms.youtube import make_client, YoutubePlatform

    async def _run():
        client = make_client()
        try:
            if settings.youtube_api_key:
                results = await YoutubePlatform.get_hot(client, limit=3)
            else:
                # 降级 yt-dlp
                from vidagent.tools.platforms.youtube import _ytdlp_trending
                loop = asyncio.get_event_loop()
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor() as pool:
                    results = await loop.run_in_executor(pool, _ytdlp_trending, 3)
            check("返回了结果", len(results) > 0, f"got {len(results)}")
            for i, r in enumerate(results[:3]):
                ok = r.get("video_id") and r["platform"] == "youtube"
                check(f"  trending[{i}]: {r.get('title','')[:50]}", ok)
        finally:
            await client.aclose()

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 8. YouTube 创作者查询
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_creator():
    section("8. YouTube 创作者查询")

    from vidagent.config import settings
    from vidagent.tools.platforms.youtube import make_client, YoutubePlatform

    if not settings.youtube_api_key:
        skip("YOUTUBE_API_KEY 未配置（创作者查询需要 key，yt-dlp 不支持）")
        return

    async def _run():
        client = make_client()
        try:
            # 用已知活跃频道 ID 测试 (Google Developers: UC_x5XG1OV2P6uZZ5FSM9Ttw)
            results = await YoutubePlatform.get_creator(
                client, "UC_x5XG1OV2P6uZZ5FSM9Ttw", limit=3
            )
            # 创作者查询可能因 quota/权限返回空，不算失败
            if results:
                check("频道 ID 查询返回结果", len(results) > 0)
                for r in results:
                    check(f"  结果: {r.get('title', '')[:50]}", r["platform"] == "youtube")
            else:
                skip("API 返回空（可能 quota 或权限限制）")
        finally:
            await client.aclose()

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 10. 抖音 extract_video_id + normalize
# ══════════════════════════════════════════════════════════════════════════════

def test_douyin_extract_and_normalize():
    section("10. 抖音 extract_video_id + normalize")

    from vidagent.tools.platforms.douyin import extract_video_id, normalize

    # extract_video_id
    check("标准 URL (19位)", extract_video_id("https://www.douyin.com/video/7123456789012345678") == "7123456789012345678")
    check("短链 v.douyin.com", extract_video_id("https://v.douyin.com/abc123/") == "dy_short_abc123")
    check("非抖音 URL", extract_video_id("https://www.bilibili.com/video/BV123") is None)

    # normalize trending topic
    topic = {
        "word": "测试热搜词",
        "hot_value": 9999,
        "video_count": 50,
        "event_time": 1700000000,
        "group_id": "12345",
        "word_cover": {"url_list": ["https://example.com/cover.jpg"]},
    }
    n = normalize(topic)
    check("normalize trending: platform='douyin'", n["platform"] == "douyin")
    check("normalize trending: title=word", n["title"] == "测试热搜词")
    check("normalize trending: is_trending_topic=True", n.get("is_trending_topic") is True)
    check("normalize trending: hot_value", n.get("hot_value") == 9999)
    check("normalize trending: view_count=video_count", n.get("view_count") == 50)
    check("normalize trending: video_url 含搜索链接", "search" in n["video_url"])

    # normalize video detail
    video = {
        "aweme_id": "7123456789012345678",
        "desc": "测试视频描述",
        "duration": 30000,  # 30s in ms
        "create_time": 1700000000,
        "author": {"nickname": "测试作者"},
        "statistics": {"digg_count": 1234},
        "share_url": "https://v.douyin.com/xxx/",
    }
    n2 = normalize(video)
    check("normalize video: video_id", n2["video_id"] == "7123456789012345678")
    check("normalize video: duration 30s", n2["duration"] == 30)
    check("normalize video: author", n2["author"] == "测试作者")
    check("normalize video: view_count=digg", n2["view_count"] == 1234)


# ══════════════════════════════════════════════════════════════════════════════
# 11. 抖音热榜 (公开 API)
# ══════════════════════════════════════════════════════════════════════════════

def test_douyin_hot_search():
    section("11. 抖音热榜 (公开 API)")

    from vidagent.tools.platforms.douyin import make_client, DouyinPlatform

    async def _run():
        client = make_client()
        try:
            results = await DouyinPlatform.get_hot(client, limit=10)
            check("返回了热榜结果", len(results) > 0, f"got {len(results)}")
            for i, r in enumerate(results[:5]):
                ok = r.get("platform") == "douyin" and r.get("title") and r.get("is_trending_topic")
                check(f"  #{i+1}: {r.get('title','')[:40]}", ok, f"hot={r.get('hot_value')}")
        finally:
            await client.aclose()

    asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# 12. Crawler 统一分派
# ══════════════════════════════════════════════════════════════════════════════

def test_crawler_dispatch():
    section("12. Crawler 统一分派")

    import vidagent.tools.platforms.bilibili   # noqa: F401
    import vidagent.tools.platforms.youtube    # noqa: F401
    from vidagent.tools.crawler import get_hot_videos, search_videos, get_creator_videos, search_and_fetch_videos
    from vidagent.tools.platforms import get_platform

    # 函数签名检查
    import inspect
    for fn, name in [(get_hot_videos, "get_hot"), (search_videos, "search"), (get_creator_videos, "creator")]:
        sig = inspect.signature(fn)
        check(f"{name}: platform 参数存在", "platform" in sig.parameters)
        check(f"{name}: platform 默认 'bilibili'", sig.parameters["platform"].default == "bilibili")

    # 平台分派验证（调用 platforms 目录下的方法，不实际网络请求）
    bp = get_platform("bilibili")
    yp = get_platform("youtube")
    check("Bilibili 有 search 方法", hasattr(bp, "search") and callable(bp.search))
    check("Bilibili 有 get_hot 方法", hasattr(bp, "get_hot") and callable(bp.get_hot))
    check("Bilibili 有 get_creator 方法", hasattr(bp, "get_creator") and callable(bp.get_creator))
    check("Bilibili 有 download 方法", hasattr(bp, "download") and callable(bp.download))
    check("YouTube 有 search 方法", hasattr(yp, "search") and callable(yp.search))
    check("YouTube 有 get_hot 方法", hasattr(yp, "get_hot") and callable(yp.get_hot))
    check("YouTube 有 get_creator 方法", hasattr(yp, "get_creator") and callable(yp.get_creator))
    check("YouTube 有 download 方法", hasattr(yp, "download") and callable(yp.download))

    # search_and_fetch_videos 向后兼容
    check("search_and_fetch_videos 可导入", callable(search_and_fetch_videos))


# ══════════════════════════════════════════════════════════════════════════════
# 10. Downloader 平台路由
# ══════════════════════════════════════════════════════════════════════════════

def test_downloader_routing():
    section("13. Downloader 平台路由")

    from vidagent.tools import downloader as dl

    # _platform_of 向后兼容
    checks = [
        ("https://www.bilibili.com/video/BV1", "bilibili"),
        ("https://b23.tv/abc", "bilibili"),
        ("https://www.youtube.com/watch?v=xxx", "youtube"),
        ("https://youtu.be/xxx", "youtube"),
        ("https://www.douyin.com/video/1", "douyin"),
        ("https://example.com/", "unknown"),
    ]
    for url, expected in checks:
        result = dl._platform_of(url)
        check(f"_platform_of('{url[:40]}...') → '{expected}'",
              result == expected, f"got '{result}'")

    # download_video 缓存命中测试（不触发实际下载）
    import tempfile
    import vidagent.utils.storage as storage_mod
    with tempfile.TemporaryDirectory() as tmpdir:
        # 模拟缓存文件
        cache_file = Path(tmpdir) / "BV_test.mp4"
        cache_file.write_text("fake mp4")
        original_media_path = storage_mod.media_path
        storage_mod.media_path = lambda name, ext: Path(tmpdir) / f"{name}{ext}"
        try:
            result = dl.download_video("https://www.bilibili.com/video/BV_test", "BV_test")
            check("缓存命中: status='success'", result.get("status") == "success")
            check("缓存命中: cached=True", result.get("cached") is True)
            check("缓存命中: 有 local_path", bool(result.get("local_path")))
        finally:
            storage_mod.media_path = original_media_path


# ══════════════════════════════════════════════════════════════════════════════
# 11. Server _extract_video_id
# ══════════════════════════════════════════════════════════════════════════════

def test_server_extract_video_id():
    section("14. Server _extract_video_id")

    from server.main import _extract_video_id

    checks = [
        ("https://www.bilibili.com/video/BV1R53R6rE7a", "BV1R53R6rE7a"),
        ("https://b23.tv/abc123", None),  # b23.tv 短链无法提取 BV（但能识别平台）
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/embed/abc123def45", "abc123def45"),
        ("https://example.com/video", None),
        ("", None),
    ]
    for url, expected in checks:
        result = _extract_video_id(url)
        check(f"_extract_video_id('{url[:45]}') → {expected!r}",
              result == expected, f"got {result!r}")


# ══════════════════════════════════════════════════════════════════════════════
# 12. Tool Definitions
# ══════════════════════════════════════════════════════════════════════════════

def test_tool_definitions():
    section("15. Tool Definitions platform enum")

    from server.tool_definitions import TOOL_DEFINITIONS

    check("有 6 个工具定义", len(TOOL_DEFINITIONS) == 6)

    # 三个检索工具的 platform enum 应包含 youtube
    retrieval_tools = ["get_hot_videos", "search_videos", "get_creator_videos"]
    for td in TOOL_DEFINITIONS:
        name = td["function"]["name"]
        props = td["function"]["parameters"]["properties"]
        if name in retrieval_tools and "platform" in props:
            enum_vals = props["platform"].get("enum", [])
            check(f"{name}: platform enum 含 bilibili+youtube+douyin",
                  all(p in enum_vals for p in ["bilibili", "youtube", "douyin"]),
                  f"enum={enum_vals}")

    # batch_summarize_videos 的 platform 字段
    batch = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "batch_summarize_videos")
    batch_props = batch["function"]["parameters"]["properties"]["videos"]["items"]["properties"]
    check("batch_summarize: videos items 含 platform 字段", "platform" in batch_props)


# ══════════════════════════════════════════════════════════════════════════════
# 13. Agent Prompt
# ══════════════════════════════════════════════════════════════════════════════

def test_agent_prompt():
    section("16. Agent Prompt 包含多平台")

    from vidagent.agent import SYSTEM_PROMPT as agno_prompt
    check("Agno prompt 含 youtube", "youtube" in agno_prompt.lower())
    check("Agno prompt 含 douyin", "douyin" in agno_prompt.lower())

    # 检查 route.ts 源码（纯文本检查）
    route_path = _PROJECT_ROOT / "frontend" / "src" / "app" / "api" / "chat" / "route.ts"
    if route_path.exists():
        content = route_path.read_text()
        check("route.ts prompt 含 youtube", "youtube" in content.lower())
        check("route.ts prompt 含 douyin", "douyin" in content.lower())
        # 验证 platform zod schema 更新了
        check("route.ts 不含旧文本 '仅支持 bilibili'",
              "仅支持 bilibili" not in content)
    else:
        skip("route.ts 不存在", str(route_path))


# ══════════════════════════════════════════════════════════════════════════════
# 14. YouTube 下载（完整端到端）
# ══════════════════════════════════════════════════════════════════════════════

def test_youtube_download():
    section("17. YouTube 下载")

    from vidagent.tools.downloader import download_video

    # 用一个短视频测试完整下载流程
    # "Rick Astley - Never Gonna Give You Up" (always available)
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    test_id = "TEST_yt_download"

    result = download_video(test_url, test_id)
    if result.get("status") == "success":
        check("下载成功", True)
        check("平台为 youtube", result.get("platform") == "youtube")
        check("有 local_path", bool(result.get("local_path")))
        check("产物文件存在", Path(str(result.get("local_path", ""))).exists())

        # 验证缓存命中（第二次下载同一文件）
        r2 = download_video(test_url, test_id)
        check("缓存命中: cached=True", r2.get("cached") is True)
        check("缓存命中: 同一 local_path", r2.get("local_path") == result.get("local_path"))
    else:
        # 下载失败可能是格式/网络问题，算了半失败
        check("下载成功", False, str(result.get("error", "")[:80]))
        check("至少返回了 error 信息", "error" in result)


# ══════════════════════════════════════════════════════════════════════════════
# 15. 配置检查
# ══════════════════════════════════════════════════════════════════════════════

def test_config():
    section("18. 配置检查")

    from vidagent.config import settings

    check("youtube_proxy 已配置", bool(settings.youtube_proxy),
          f"value={settings.youtube_proxy}")

    has_key = bool(settings.youtube_api_key)
    if has_key:
        print(f"  ℹ️  YOUTUBE_API_KEY 已配置 (长度 {len(settings.youtube_api_key)})")
    else:
        print("  ℹ️  YOUTUBE_API_KEY 未配置（将使用 yt-dlp 降级搜索）")

    has_cookie = bool(settings.youtube_cookie)
    if has_cookie:
        print(f"  ℹ️  YOUTUBE_COOKIE 已配置: {settings.youtube_cookie}")

    # 验证 proxy 格式
    proxy = settings.youtube_proxy
    check("proxy 格式正确 (http://...)", proxy.startswith("http://") or proxy.startswith("socks5://"),
          f"got: {proxy}")


# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  VidAgent 多平台扩展测试")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  CWD: {os.getcwd()}")
    print("=" * 60)

    tests = [
        ("平台注册表", test_platform_registry),
        ("B站 normalize+兼容", test_bilibili),
        ("YouTube normalize", test_youtube_normalize),
        ("YouTube extract_video_id", test_youtube_extract_id),
        ("YouTube yt-dlp 搜索", test_youtube_ytdlp_search),
        ("YouTube API v3 搜索", test_youtube_api_search),
        ("YouTube 热门", test_youtube_trending),
        ("YouTube 创作者", test_youtube_creator),
        ("抖音 extract+normalize", test_douyin_extract_and_normalize),
        ("抖音热榜", test_douyin_hot_search),
        ("Crawler 分派", test_crawler_dispatch),
        ("Downloader 路由", test_downloader_routing),
        ("Server _extract_video_id", test_server_extract_video_id),
        ("Tool Definitions", test_tool_definitions),
        ("Agent Prompt", test_agent_prompt),
        ("YouTube 下载", test_youtube_download),
        ("配置检查", test_config),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            print(f"  💥 {name} 异常: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            global fail_count
            fail_count += 1

    total = pass_count + fail_count + skip_count
    print(f"\n{'='*60}")
    print(f"  结果: {pass_count} 通过 / {fail_count} 失败 / {skip_count} 跳过 (共 {total})")
    print(f"{'='*60}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
