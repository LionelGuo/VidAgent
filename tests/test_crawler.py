import pytest


@pytest.mark.asyncio
async def test_get_creator_videos_resolves_name(monkeypatch):
    """昵称应被自动解析为 mid，再用于拉取投稿。"""
    from vidagent.tools import bilibili, crawler

    resolved: dict = {}
    fetched: dict = {}

    async def fake_resolve(_client, name):
        resolved["name"] = name
        return ("546195", "老番茄", 20624579)

    async def fake_fetch(_client, mid, ps=30, **_kw):
        fetched["mid"] = mid
        return [{"video_id": "BV1", "author": "老番茄", "title": "t", "duration": 60}]

    monkeypatch.setattr(bilibili, "resolve_creator_mid", fake_resolve)
    monkeypatch.setattr(bilibili, "fetch_user_videos", fake_fetch)

    items = await crawler.get_creator_videos("bilibili", "老番茄", limit=3)
    assert resolved["name"] == "老番茄"
    assert fetched["mid"] == "546195"
    assert items[0]["author"] == "老番茄"


@pytest.mark.asyncio
async def test_get_creator_videos_numeric_skips_resolution(monkeypatch):
    """数字 UID 应直接使用，不触发昵称解析。"""
    from vidagent.tools import bilibili, crawler

    fetched: dict = {}
    resolve_called = {"v": False}

    async def fake_resolve(*_a, **_k):
        resolve_called["v"] = True
        return ("x", "y", 0)

    async def fake_fetch(_client, mid, ps=30, **_kw):
        fetched["mid"] = mid
        return []

    monkeypatch.setattr(bilibili, "resolve_creator_mid", fake_resolve)
    monkeypatch.setattr(bilibili, "fetch_user_videos", fake_fetch)

    await crawler.get_creator_videos("bilibili", "546195", limit=3)
    assert fetched["mid"] == "546195"
    assert resolve_called["v"] is False


@pytest.mark.asyncio
async def test_search_and_fetch_videos_backcompat_dispatch(monkeypatch):
    """旧入口按 task_type 正确分派到新工具（pipeline/crawl_cli 仍可用）。"""
    from vidagent.tools import crawler

    async def fake_hot(platform="bilibili", limit=10, date_filter=None):
        return [{"video_id": "BV", "duration": 10}]

    async def fake_search(platform="bilibili", keyword="", limit=10, date_filter=None):
        return [{"video_id": "BV", "duration": 20, "keyword": keyword}]

    monkeypatch.setattr(crawler, "get_hot_videos", fake_hot)
    monkeypatch.setattr(crawler, "search_videos", fake_search)

    hot = await crawler.search_and_fetch_videos("bilibili", "hot_board", limit=1)
    assert hot[0]["duration"] == 10

    sr = await crawler.search_and_fetch_videos("bilibili", "search", "kw", limit=1)
    assert sr[0]["duration"] == 20
    assert sr[0]["keyword"] == "kw"
