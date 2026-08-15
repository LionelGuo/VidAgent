"""日期/时间过滤工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 展示时区：Asia/Shanghai 无夏令时、恒 UTC+8（中国自 1991 年起无 DST）。
# 用固定偏移而非 zoneinfo——Docker 精简镜像可能缺 tzdata 数据库。
# 全平台统一此展示时区（含 YouTube：其官方 UI 按观看者本地时区显示，
# 中文用户看到的就是北京时间；统一时区保证跨平台日期一致可比）。
_CST = timezone(timedelta(hours=8))


def format_publish_date(ts: int) -> str:
    """unix 秒 → 'YYYY-MM-DD'（北京时区，与中文平台 app 显示一致）。

    B17（2026-08-15）：模型不擅长 epoch→日期算术（实测把 1786698013
    换算成「2025-08-15」，实际 2026-08-14，年份错 1）——日期由后端预
    格式化为字符串进 wire，模型直接展示即可。
    """
    return datetime.fromtimestamp(int(ts), _CST).strftime("%Y-%m-%d")


def start_of_today_cst() -> int:
    """返回北京时区「今日 00:00」的 unix 时间戳（秒）。

    与 format_publish_date 同用 _CST——「今天发布」的过滤边界与展示
    时区一致。曾用服务器本地时区（time.localtime）：Docker UTC 主机上
    边界错位 8 小时，「只看今天」会静默漏掉北京时间 00:00-07:59 发布
    的条目（其 publish_date 却显示为今天）。
    """
    now = datetime.now(_CST)
    return int(datetime(now.year, now.month, now.day, tzinfo=_CST).timestamp())


def filter_today(items: list[dict], ts_key: str = "publish_time") -> list[dict]:
    """过滤出时间戳 >= 今日 0 点（北京时区）的项；过滤后为空返回空列表（诚实空态）。

    B12（2026-08-15）：曾「空则原样返回」——静默回退未过滤全量使
    「今天的热榜」请求返回陈旧数据、模型如实读出条目日期后误报
    「这是8月8日的数据」。空态交给调用方/模型如实呈现。
    """
    cutoff = start_of_today_cst()
    return [x for x in items if int(x.get(ts_key, 0) or 0) >= cutoff]
