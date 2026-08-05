"""轻量计时器：定位耗时瓶颈。

用法：
    with Timer("ASR 转写"):
        ...
    # 退出时打印：⏱ ASR 转写 耗时 12.34s

或装饰器（自动适配 sync/async）：
    @timed("视频下载")
    def download_video(...): ...
"""

from __future__ import annotations

import inspect
import logging
import time
from functools import wraps

logger = logging.getLogger("vidagent.timer")


class Timer:
    """计时段落。`elapsed` 属性可在退出后读取。"""

    def __init__(self, name: str):
        self.name = name
        self.elapsed = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.elapsed = time.perf_counter() - self._t0
        logger.info("⏱ %s 耗时 %.2fs", self.name, self.elapsed)


def timed(name: str):
    """函数计时装饰器（自动适配 sync / async 函数）。"""

    def deco(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def aw(*args, **kwargs):
                with Timer(name):
                    return await fn(*args, **kwargs)

            return aw

        @wraps(fn)
        def sw(*args, **kwargs):
            with Timer(name):
                return fn(*args, **kwargs)

        return sw

    return deco
