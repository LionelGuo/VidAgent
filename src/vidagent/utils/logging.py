"""统一日志配置：唯一入口 setup_logging()，模板见 docs/后端日志规范.md。

输出形态：
  2026-08-14 21:30:05 | INFO   | douyin | 抖音搜索 'Python教程': 3 条

规则：
- 消息仅允许中文汉字 + ASCII 可打印字符（无 emoji/箭头/全角标点）
- 调用点保持 % 参数化惰性格式化，禁止 f-string 拼接日志
- 第三方噪声（httpx/openai/urllib3/httpcore）压至 WARNING
"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s | %(levelname)-7s | %(tag)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# logger 全名 → 展示短标识符；未知 logger 兜底用全名（不漏日志）。
# 新平台接入时在此加一行（如未来 weibo）。
LOG_TAGS: dict[str, str] = {
    "server.main": "api",
    "server.sse_relay": "sse",
    "vidagent.tools.platforms": "platforms",
    "vidagent.tools.platforms.douyin": "douyin",
    "vidagent.tools.platforms.xiaohongshu": "xhs",
    "vidagent.tools.platforms.kuaishou": "kuaishou",
    "vidagent.tools.platforms.bilibili": "bilibili",
    "vidagent.tools.platforms.youtube": "youtube",
    "vidagent.tools.platforms._cdp_browser": "cdp",
    "vidagent.tools.platforms._mediacrawler": "mediacrawler",
    "vidagent.tools.summarize.pipeline": "summarize.pipeline",
    "vidagent.tools.summarize.transport": "summarize.transport",
    "vidagent.tools.summarize.multimodal": "summarize.multimodal",
    "vidagent.tools.summarize.short_video": "summarize.short_video",
    "vidagent.tools.downloader": "download",
    "vidagent.tools.crawler": "crawler",
    "vidagent.utils.audio": "audio",
    "vidagent.utils.frames": "frames",
    "vidagent.timer": "timer",
}

_NOISY_LOGGERS = ("httpx", "openai", "urllib3", "httpcore")


class _TagFormatter(logging.Formatter):
    """按 logger 全名注入短标识符（未知 logger 兜底全名）。"""

    def format(self, record: logging.LogRecord) -> str:
        record.tag = LOG_TAGS.get(record.name, record.name)
        return super().format(record)


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志（统一模板 + 短标识符），并收敛嘈杂的第三方日志。"""
    handler = logging.StreamHandler()
    handler.setFormatter(_TagFormatter(_FORMAT, datefmt=_DATEFMT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
