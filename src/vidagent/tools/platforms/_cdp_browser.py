"""共享 CDP 浏览器管理：连接 Windows Chrome 远程调试端口。

所有需要 Playwright 的平台（douyin/kuaishou/xiaohongshu）
通过此模块共享同一个浏览器 context，复用用户的登录态。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MEDIACRAWLER_ROOT = str(Path.home() / "Code" / "MediaCrawler")
_mc_venv = str(Path(_MEDIACRAWLER_ROOT) / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
if not os.path.isdir(_mc_venv):
    _venv_lib = Path(_MEDIACRAWLER_ROOT) / ".venv" / "lib"
    _candidates = sorted(_venv_lib.glob("python*/site-packages")) if _venv_lib.exists() else []
    _mc_venv = str(_candidates[0]) if _candidates else ""
for _p in [_mc_venv, _MEDIACRAWLER_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

_original_cwd = os.getcwd()

# 单例
_cdp_manager = None
_browser_context = None
_page_cache: dict[str, Any] = {}  # platform → page
_initialized = False


async def get_cdp_context():
    """获取或初始化 CDP 浏览器 context（连接 Windows Chrome :9222）。"""
    global _cdp_manager, _browser_context, _initialized

    if _initialized and _browser_context is not None:
        return _browser_context

    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        import config as mc_config
        mc_config.ENABLE_CDP_MODE = True
        mc_config.CDP_CONNECT_EXISTING = True
        mc_config.CDP_DEBUG_PORT = 9222
        mc_config.CDP_HEADLESS = False
        mc_config.AUTO_CLOSE_BROWSER = False
        mc_config.SAVE_LOGIN_STATE = True
        mc_config.HEADLESS = False

        from playwright.async_api import async_playwright
        from tools.cdp_browser import CDPBrowserManager
    finally:
        os.chdir(_original_cwd)

    _playwright = await async_playwright().start()
    _cdp_manager = CDPBrowserManager()
    _browser_context = await _cdp_manager.launch_and_connect(
        playwright=_playwright,
        playwright_proxy=None,
        user_agent=None,
        headless=False,
    )
    await _cdp_manager.add_stealth_script()
    _initialized = True
    logger.info("CDP 浏览器已连接 (Windows Chrome :9222)")
    return _browser_context


async def get_page_for_platform(platform: str, url: str) -> Any:
    """获取指定平台的 Playwright Page（已导航到目标 URL）。"""
    global _page_cache

    if platform in _page_cache:
        page = _page_cache[platform]
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass

    ctx = await get_cdp_context()
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    _page_cache[platform] = page
    return page


def get_mc_utils():
    """获取 MediaCrawler 的 utils 模块。"""
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        from tools import utils as mc_utils
        return mc_utils
    finally:
        os.chdir(_original_cwd)


def chdir_mc():
    """临时切换 cwd 到 MediaCrawler 根（用于 execjs 等相对路径依赖）。"""
    return _MEDIACRAWLER_ROOT
