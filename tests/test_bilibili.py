from vidagent.tools import bilibili as b
from vidagent.utils import wbi


def test_normalize_popular_shape():
    item = {
        "bvid": "BV1xx", "title": "T", "desc": "D", "pubdate": 1700000000,
        "owner": {"name": "UP"}, "stat": {"view": 123},
    }
    n = b.normalize(item)
    assert n["video_id"] == "BV1xx"
    assert n["author"] == "UP"
    assert n["view_count"] == 123
    assert n["video_url"].endswith("/BV1xx")
    assert n["platform"] == "bilibili"


def test_normalize_search_strips_html_and_author_string():
    item = {
        "bvid": "BVyy", "title": '<em class="keyword">K</em>', "description": "DD",
        "pubdate": 1700000000, "author": "UP2", "play": 999,
    }
    n = b.normalize(item)
    assert n["title"] == "K"
    assert n["desc"] == "DD"
    assert n["author"] == "UP2"
    assert n["view_count"] == 999


def test_normalize_user_vlist_shape():
    item = {
        "bvid": "BVzz", "title": "T3", "description": "D3",
        "created": 1700000000, "author": "UP3", "play": 5,
    }
    n = b.normalize(item)
    assert n["publish_time"] == 1700000000
    assert n["author"] == "UP3"


def test_normalize_duration_int_seconds():
    n = b.normalize({"bvid": "BV1", "title": "t", "duration": 149})
    assert n["duration"] == 149
    assert n["duration_text"] == "02:29"


def test_normalize_duration_mss_string():
    n = b.normalize({"bvid": "BV2", "title": "t", "duration": "11:41"})
    assert n["duration"] == 701
    assert n["duration_text"] == "11:41"


def test_normalize_duration_hmmss_string():
    n = b.normalize({"bvid": "BV3", "title": "t", "duration": "1:02:03"})
    assert n["duration"] == 3723
    assert n["duration_text"] == "1:02:03"


def test_normalize_duration_from_length_field():
    """创作者 vlist 用 length 字段而非 duration。"""
    n = b.normalize({"bvid": "BV4", "title": "t", "length": "10:30"})
    assert n["duration"] == 630
    assert n["duration_text"] == "10:30"


def test_wbi_mixin_key_length_and_deterministic():
    s = "0123456789abcdef" * 4  # 64 chars
    k = wbi._get_mixin_key(s)
    assert len(k) == 32
    assert wbi._get_mixin_key(s) == wbi._get_mixin_key(s)


def test_check_raises_on_error_code():
    import pytest

    with pytest.raises(b.BiliAPIError):
        b._check({"code": -352, "message": "风控"}, "test")


def test_check_passes_on_zero():
    assert b._check({"code": 0, "data": {}}, "test")["code"] == 0


def test_normalize_user():
    u = b._normalize_user(
        {"mid": 546195, "uname": "老番茄", "fans": "123", "level": 6, "face": "f"}
    )
    assert u["mid"] == "546195"
    assert u["uname"] == "老番茄"
    assert u["fans"] == 123
    assert u["level"] == 6


def test_pick_best_user_prefers_exact_name():
    # 精确匹配优先，即便其 fans 不是最大
    users = [
        {"mid": "1", "uname": "老番茄2", "fans": 999},
        {"mid": "546195", "uname": "老番茄", "fans": 100},
        {"mid": "2", "uname": "X老番茄", "fans": 500},
    ]
    assert b._pick_best_user(users, "老番茄")["mid"] == "546195"


def test_pick_best_user_no_exact_picks_top_fans():
    users = [{"mid": "1", "uname": "A", "fans": 10}, {"mid": "2", "uname": "B", "fans": 99}]
    assert b._pick_best_user(users, "不存在")["mid"] == "2"


def test_pick_best_user_empty():
    assert b._pick_best_user([], "x") is None
