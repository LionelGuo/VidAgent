import pytest


@pytest.mark.asyncio
async def test_user_homepage_resolves_name_to_mid(monkeypatch):
    """昵称应被自动解析为 mid，再用于拉取投稿。"""
    from vidagent.tools import bilibili, crawler

    resolved_with: dict = {}
    fetched_with: dict = {}

    async def fake_resolve(_client, name):
        resolved_with["name"] = name
        return ("546195", "老番茄", 20624579)

    async def fake_fetch(_client, mid, ps=30, **_kw):
        fetched_with["mid"] = mid
        return [{"video_id": "BV1", "author": "老番茄", "title": "t"}]

    monkeypatch.setattr(bilibili, "resolve_creator_mid", fake_resolve)
    monkeypatch.setattr(bilibili, "fetch_user_videos", fake_fetch)

    items = await crawler._bilibili("user_homepage", "老番茄", None, 3)
    assert resolved_with["name"] == "老番茄"
    assert fetched_with["mid"] == "546195"
    assert items[0]["author"] == "老番茄"


@pytest.mark.asyncio
async def test_user_homepage_numeric_mid_skips_resolution(monkeypatch):
    """数字 UID 应直接使用，不触发昵称解析。"""
    from vidagent.tools import bilibili, crawler

    fetched_with: dict = {}
    resolve_called = {"v": False}

    async def fake_resolve(*_a, **_k):
        resolve_called["v"] = True
        return ("x", "y", 0)

    async def fake_fetch(_client, mid, ps=30, **_kw):
        fetched_with["mid"] = mid
        return []

    monkeypatch.setattr(bilibili, "resolve_creator_mid", fake_resolve)
    monkeypatch.setattr(bilibili, "fetch_user_videos", fake_fetch)

    await crawler._bilibili("user_homepage", "546195", None, 3)
    assert fetched_with["mid"] == "546195"
    assert resolve_called["v"] is False
