"""日期/时间过滤工具。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# 展示时区：Asia/Shanghai 无夏令时、恒 UTC+8（中国自 1991 年起无 DST）。
# 用固定偏移而非 zoneinfo——Docker 精简镜像可能缺 tzdata 数据库。
_CST = timezone(timedelta(hours=8))


def format_publish_date(ts: int) -> str:
    """unix 秒 → 'YYYY-MM-DD'（北京时区，与中文平台 app 显示一致）。

    B17（2026-08-15）：模型不擅长 epoch→日期算术（实测把 1786698013
    换算成「2025-08-15」，实际 2026-08-14，年份错 1）——日期由后端预
    格式化为字符串进 wire，模型直接展示即可。
    """
    return datetime.fromtimestamp(int(ts), _CST).strftime("%Y-%m-%d")


def start_of_today_local() -> int:
    """返回本地时区「今日 00:00」对应的 unix 时间戳（秒）。"""
    lt = time.localtime()
    return int(
        time.mktime(time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    )


def filter_today(items: list[dict], ts_key: str = "publish_time") -> list[dict]:
    """过滤出时间戳 >= 今日 0 点的项；过滤后为空返回空列表（诚实空态）。

    B12（2026-08-15）：曾「空则原样返回」——静默回退未过滤全量使
    「今天的热榜」请求返回陈旧数据、模型如实读出条目日期后误报
    「这是8月8日的数据」。空态交给调用方/模型如实呈现。
    """
    cutoff = start_of_today_local()
    return [x for x in items if int(x.get(ts_key, 0) or 0) >= cutoff]
