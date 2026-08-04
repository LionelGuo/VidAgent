from vidagent.utils import storage


def test_sanitize_keeps_cjk_and_alnum():
    assert storage.sanitize("大家好abc123") == "大家好abc123"


def test_sanitize_strips_unsafe_and_spaces():
    assert storage.sanitize("../../etc") == "etc"
    assert storage.sanitize("a b:c*d") == "abcd"  # 空格/冒号/星号被去掉


def test_sanitize_empty_fallback():
    assert storage.sanitize("") == "video"
    assert storage.sanitize("///") == "video"


def test_media_path_under_workspace():
    p = storage.media_path("标题", ".mp4")
    assert p.suffix == ".mp4"
    assert p.parent == storage.workspace()
