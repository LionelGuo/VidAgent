"""日期/时间过滤工具。"""

from __future__ import annotations

import time


def start_of_today_local() -> int:
    """返回本地时区「今日 00:00」对应的 unix 时间戳（秒）。"""
    lt = time.localtime()
    return int(
        time.mktime(time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    )


def filter_today(items: list[dict], ts_key: str = "publish_time") -> list[dict]:
    """过滤出时间戳 >= 今日 0 点的项；若过滤后为空则原样返回（避免空集）。"""
    cutoff = start_of_today_local()
    today = [x for x in items if int(x.get(ts_key, 0) or 0) >= cutoff]
    return today if today else items
