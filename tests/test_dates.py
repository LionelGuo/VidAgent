import time

from vidagent.utils import dates


def test_filter_today_empty_falls_back():
    items = [{"publish_time": 1}, {"publish_time": 2}]
    assert dates.filter_today(items) == items  # 全是旧数据 → 原样返回


def test_filter_today_keeps_recent():
    now = int(time.time())
    items = [{"publish_time": 1}, {"publish_time": now}]
    out = dates.filter_today(items)
    assert len(out) == 1 and out[0]["publish_time"] == now
