"""共享 CDP 浏览器管理：连接 Windows Chrome 远程调试端口。

所有需要 Playwright 的平台（douyin/kuaishou/xiaohongshu）
通过此模块共享同一个浏览器 context，复用用户的登录态。

架构关键：Playwright 的异步对象绑定创建时的事件循环。平台 download()
是同步入口（每次调用 asyncio.run 创建新循环），若跨调用复用模块级
Playwright 单例，第二次调用就会在已关闭的循环上调度回调（
"Event loop is closed"）。因此本模块维护一个 CDP 专用常驻事件循环线程，
所有 Playwright 操作一律通过 run_on_cdp_loop / run_on_cdp_loop_async
提交到该循环执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
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

# ── CDP 专用常驻事件循环（所有 Playwright 对象归属此循环）──
_cdp_loop: asyncio.AbstractEventLoop | None = None
_cdp_loop_thread: threading.Thread | None = None


def _get_cdp_loop() -> asyncio.AbstractEventLoop:
    """获取（或创建）CDP 专用事件循环，跑在后台守护线程上。"""
    global _cdp_loop, _cdp_loop_thread
    if _cdp_loop is None or _cdp_loop.is_closed():
        _cdp_loop = asyncio.new_event_loop()
        _cdp_loop_thread = threading.Thread(
            target=_cdp_loop.run_forever,
            daemon=True,
            name="vidagent-cdp-loop",
        )
        _cdp_loop_thread.start()
    return _cdp_loop


def run_on_cdp_loop(coro: Any) -> Any:
    """同步入口：把协程提交到 CDP 循环并阻塞等待结果。

    供平台 download()（在线程池中同步调用）使用——不再用 asyncio.run
    创建临时循环，避免跨循环复用 Playwright 单例。
    """
    loop = _get_cdp_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


async def run_on_cdp_loop_async(coro: Any) -> Any:
    """异步入口：从其他事件循环（uvicorn 主循环）提交协程到 CDP 循环。

    供平台 search()/get_creator()（异步接口）使用。
    """
    loop = _get_cdp_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return await asyncio.wrap_future(future)


def _reset_cdp_state() -> None:
    """重置 CDP 连接状态（浏览器被关闭后重新连接）。不关闭浏览器。"""
    global _cdp_manager, _browser_context, _page_cache, _initialized
    _cdp_manager = None
    _browser_context = None
    _page_cache = {}
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
        # VidAgent 缩短连接等待：Chrome 要么在要么不在，15s 足够
        mc_config.BROWSER_LAUNCH_TIMEOUT = 15

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
    """获取指定平台的 Playwright Page（已导航到目标 URL）。

    page 失效（被关闭）时自动重建；浏览器连接断开时重置 CDP 状态重连。
    """
    global _page_cache

    if platform in _page_cache:
        page = _page_cache[platform]
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass

    ctx = await get_cdp_context()
    try:
        page = await ctx.new_page()
    except Exception:
        # 浏览器可能已被关闭：重置连接状态，重新走 CDP 连接
        _reset_cdp_state()
        ctx = await get_cdp_context()
        page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    _page_cache[platform] = page
    return page


async def invalidate_page(platform: str) -> None:
    """关闭指定平台的缓存 page（若存在）并从缓存移除。

    仅关闭 VidAgent 自己打开的标签页，绝不关闭用户浏览器 context。
    """
    global _page_cache
    page = _page_cache.pop(platform, None)
    if page is not None:
        try:
            await page.close()
        except Exception:
            pass


_mc_utils = None


def get_mc_utils():
    """获取 MediaCrawler 的 utils 模块（导入一次后缓存）。"""
    global _mc_utils
    if _mc_utils is not None:
        return _mc_utils
    os.chdir(_MEDIACRAWLER_ROOT)
    try:
        from tools import utils as mc_utils
        _mc_utils = mc_utils
        return mc_utils
    finally:
        os.chdir(_original_cwd)


def chdir_mc():
    """临时切换 cwd 到 MediaCrawler 根（用于 execjs 等相对路径依赖）。"""
    return _MEDIACRAWLER_ROOT
