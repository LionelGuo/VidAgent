from vidagent.tools import downloader as dl


def test_platform_detection():
    assert dl._platform_of("https://www.bilibili.com/video/BV1") == "bilibili"
    assert dl._platform_of("https://b23.tv/abc") == "bilibili"
    assert dl._platform_of("https://www.douyin.com/video/1") == "douyin"
    assert dl._platform_of("https://www.xiaohongshu.com/explore/x") == "xiaohongshu"
    assert dl._platform_of("https://xhslink.com/a") == "xiaohongshu"
    assert dl._platform_of("https://www.kuaishou.com/xx") == "kuaishou"
    assert dl._platform_of("https://example.com/") == "unknown"


def test_download_cache_skips_ytdlp(monkeypatch, tmp_path):
    """已下载文件应直接复用，不触发 yt-dlp。"""
    import yt_dlp

    cache_file = tmp_path / "BV1.mp4"
    cache_file.write_text("cached")
    monkeypatch.setattr(
        dl.storage, "media_path", lambda name, ext: tmp_path / f"{name}{ext}"
    )

    called = {"ytdlp": False}

    class FakeYDL:
        def __init__(self, *a, **k):
            called["ytdlp"] = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, *a, **k):
            return None

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)

    r = dl.download_video("https://www.bilibili.com/video/BV1", "BV1")
    assert r["status"] == "success"
    assert r.get("cached") is True
    assert called["ytdlp"] is False

