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
