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

# MediaCrawler 源码已 vendored 入仓（vendor/MediaCrawler/，详见 ADR-0007）。
# 其 Python 依赖收敛进 VidAgent 的 [douyin] extra，无需独立 .venv——
# sys.path 只需指向 vendor root（MC 的 media_platform/ config/ tools/ 等顶层包在此）。
_REPO_ROOT = Path(__file__).resolve().parents[4]   # platforms→tools→vidagent→src→repo
_MEDIACRAWLER_ROOT = _REPO_ROOT / "vendor" / "MediaCrawler"
if str(_MEDIACRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEDIACRAWLER_ROOT))

_original_cwd = os.getcwd()

def check_mediacrawler_available() -> str | None:
    """MediaCrawler 可用返回 None；否则返回可操作的中文提示（供 CDP 平台优雅降级）。

    vendor 后依赖齐备时几乎不触发；但目录缺失、或依赖未装时，
    避免向上抛裸 ModuleNotFoundError/FileNotFoundError（调用方据此返回 [] 或 error）。
    """
    if not Path(_MEDIACRAWLER_ROOT).is_dir():
        return (
            "MediaCrawler 未就位(抖音/小红书/快手不可用)."
            "源码应 vendored 于 vendor/MediaCrawler/ (见 README)."
        )
    try:
        import execjs  # noqa: F401  # [douyin] extra 标志性依赖（douyin/help.py 顶层 import）
    except Exception:
        return (
            "MediaCrawler 已就位但 [douyin] 依赖未安装(缺 execjs/xhshow/tenacity 等)."
            "请执行 `uv sync --extra douyin` 安装(见 README)."
        )
    return None


# 单例
_cdp_manager = None
_browser_context = None
_page_cache: dict[str, Any] = {}  # platform → page
_initialized = False
# 初始化防重（B19 评审修复）：启动预热与首次真实调用可能并发初始化
# （预热等 Chrome 确认框最长 ~30s，正是调用高发窗口）——双检锁保证
# 只连一次。get_cdp_context 只在 CDP 循环上运行，锁绑定该循环。
_cdp_init_lock = asyncio.Lock()

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

    # 双检锁（B19 评审修复）：整个 init 主体在锁内——只包双检不包主体
    # 的话，两个协程可先后持锁各自 init（曾犯）
    async with _cdp_init_lock:
        if _initialized and _browser_context is not None:
            return _browser_context

        os.chdir(_MEDIACRAWLER_ROOT)
        try:
            import config as mc_config
            mc_config.ENABLE_CDP_MODE = True
            mc_config.CDP_CONNECT_EXISTING = True
            mc_config.CDP_DEBUG_PORT = 9222
            # CDP 主机（vendor 补丁）：裸机/WSL2 用默认 localhost；
            # Windows Docker（桥接网络）需 CDP_HOST=host.docker.internal（容器内 localhost ≠ 宿主）
            # 注意用 `or`：CDP_HOST= 空值也按未配置处理（避免空串被当主机名触发 IDNA 报错）
            mc_config.CDP_DEBUG_HOST = os.getenv("CDP_HOST") or "localhost"
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


async def get_page_for_platform(platform: str, url: str, wait_until: str = "domcontentloaded") -> Any:
    """获取指定平台的 Playwright Page（已导航到目标 URL）。

    page 失效（被关闭）时自动重建；浏览器连接断开时重置 CDP 状态重连。
    wait_until 默认 domcontentloaded；douyin 传 "commit"（B19 冷路径减负：
    页面就绪由 webmssdk/xmst 轮询兜底，提前 1-3s 放行）。
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
    await page.goto(url, wait_until=wait_until, timeout=15000)
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
