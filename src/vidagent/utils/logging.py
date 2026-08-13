"""统一日志配置：在入口处调用 setup_logging()，让计时/进度日志可见。"""

from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志（带时间戳），并收敛嘈杂的第三方日志。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # 第三方噪声降到 WARNING，突出我们自己的计时/进度日志
    for noisy in ("httpx", "openai", "urllib3", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
