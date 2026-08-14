"""MediaCrawler 封装层（#3 深模块）：VidAgent 与 vendored MediaCrawler 的唯一门户。

职责：
- `import_mc_platform(name, *submodules)`：统一 MC 平台模块导入（chdir 语义 + 缓存）。
  MC 唯一的导入期 cwd 依赖是 douyin/help.py 模块级 execjs 编译 libs/douyin.js——
  门户在 MC 根 cwd 下完成导入并保证恢复，调用方零 chdir 知识。
- `inject_platform_config(platform)`：平台键注入（PLATFORM/LOGIN_TYPE/ENABLE_GET_* 等）。
  MC config 是模块级全局变量；键集分区后本层是平台键的唯一写入者
  （CDP 键的唯一写入者是 _cdp_browser.get_cdp_context）——消除 #3 前
  SAVE_LOGIN_STATE/PLATFORM 双写者后写覆盖的竞态。

与 _cdp_browser 的分工：CDP 层管「连 Chrome」，本层管「用 MediaCrawler」。
未来新增 MC 平台（如微博）只需在 PLATFORM_CONFIG 加一行 + 新增适配器。
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any

from ._cdp_browser import _MEDIACRAWLER_ROOT

logger = logging.getLogger(__name__)

# 平台键注入表（key 为适配器的 Platform.name）：
# 只含平台键——CDP 键（CDP_*/HEADLESS/AUTO_CLOSE_BROWSER/BROWSER_LAUNCH_TIMEOUT/
# SAVE_LOGIN_STATE）由 _cdp_browser.get_cdp_context 统一写入，绝不在此出现。
# kuaishou 此前零写入、被动继承全局 PLATFORM（竞态源）；统一注入 "ks" 修复。
PLATFORM_CONFIG: dict[str, dict[str, Any]] = {
    "douyin": {
        "PLATFORM": "dy",
        "LOGIN_TYPE": "qrcode",
        "ENABLE_GET_MEIDAS": False,
        "ENABLE_GET_COMMENTS": False,
        "CRAWLER_MAX_NOTES_COUNT": 20,
    },
    "xiaohongshu": {
        "PLATFORM": "xhs",
        "ENABLE_GET_MEIDAS": False,
        "ENABLE_GET_COMMENTS": False,
    },
    "kuaishou": {
        "PLATFORM": "ks",
    },
}

# 已导入的 MC 平台子模块：platform → {submodule: module}
_cache: dict[str, dict[str, Any]] = {}

# 导入期 chdir 是进程全局副作用：跨平台首次导入串行化（一次性成本）
_import_lock = threading.Lock()


@contextmanager
def _chdir_mc():
    prev = os.getcwd()
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        yield
    finally:
        os.chdir(prev)


def import_mc_platform(platform: str, *submodules: str) -> dict[str, Any]:
    """在 MC 根 cwd 下导入 media_platform.<platform> 的指定子模块（缓存）。

    只导入请求的子模块：MC 平台包 __init__ 会级联 core/client，
    kuaishou 适配器只取 help（不拖 GraphQL 链）——由调用方显式声明。

    Args:
        platform: MC 平台包名（douyin/kuaishou/xhs，对应 vendor media_platform/）。
        *submodules: 子模块名（如 client/field/help）。

    Returns:
        {submodule: module}。异常时保证 cwd 已恢复。
    """
    with _import_lock:
        cached = _cache.setdefault(platform, {})
        for sub in submodules:
            if sub not in cached:
                with _chdir_mc():
                    cached[sub] = importlib.import_module(f"media_platform.{platform}.{sub}")
        return {sub: cached[sub] for sub in submodules}


def inject_platform_config(platform: str) -> None:
    """把 <platform> 的平台键写入 MC 全局 config（本层是平台键唯一写入者）。

    在 client 构造前、平台锁内调用。未知平台抛 KeyError（接入错误应尽早暴露）。
    """
    import config as mc_config

    for key, value in PLATFORM_CONFIG[platform].items():
        setattr(mc_config, key, value)
