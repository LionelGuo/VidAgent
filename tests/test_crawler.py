import pytest


@pytest.mark.asyncio
async def test_get_creator_videos_resolves_name(monkeypatch):
    """昵称应被自动解析为 mid，再用于拉取投稿。"""
    from vidagent.tools import crawler
    from vidagent.tools.platforms import bilibili

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
    from vidagent.tools import crawler
    from vidagent.tools.platforms import bilibili

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
