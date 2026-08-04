from vidagent.tools import downloader as dl


def test_platform_detection():
    assert dl._platform_of("https://www.bilibili.com/video/BV1") == "bilibili"
    assert dl._platform_of("https://b23.tv/abc") == "bilibili"
    assert dl._platform_of("https://www.douyin.com/video/1") == "douyin"
    assert dl._platform_of("https://www.xiaohongshu.com/explore/x") == "xiaohongshu"
    assert dl._platform_of("https://xhslink.com/a") == "xiaohongshu"
    assert dl._platform_of("https://www.kuaishou.com/xx") == "kuaishou"
    assert dl._platform_of("https://example.com/") == "unknown"
