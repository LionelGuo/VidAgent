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

import asyncio
import importlib
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, ClassVar

from vidagent.config import settings

from . import Platform
from ._cdp_browser import (
    _MEDIACRAWLER_ROOT,
    get_mc_utils,
    get_page_for_platform,
    invalidate_page,
    run_on_cdp_loop,
    run_on_cdp_loop_async,
)

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
    "weibo": {
        "PLATFORM": "wb",
        "ENABLE_GET_COMMENTS": False,
        # 上游自述 True 会增加被风控概率（搜索遍历后每帖再请求详情）——关
        "ENABLE_WEIBO_FULL_TEXT": False,
        "WEIBO_SEARCH_TYPE": "video",
    },
}

# 已导入的 MC 平台子模块：platform → {submodule: module}
_cache: dict[str, dict[str, Any]] = {}

# 导入期 chdir 是进程全局副作用：跨平台首次导入串行化（一次性成本）
_import_lock = threading.Lock()
# 配置注入原子性：MC config 是模块级全局变量，键集写入不可被并发拆散
_inject_lock = threading.Lock()


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

    写入在进程级锁内完成：MC config 是模块级全局变量，锁保证一次注入的
    键集原子落盘（不出现 PLATFORM/LOGIN_TYPE 拆散的中间态）；平台锁
    串行化同一平台的构造，两把锁共同消除注入竞态。
    """
    import config as mc_config

    with _inject_lock:
        for key, value in PLATFORM_CONFIG[platform].items():
            setattr(mc_config, key, value)


class MediaCrawlerPlatform(Platform):
    """MediaCrawler CDP 平台基类（#3）：MC 管理与客户端生命周期的共享模板。

    子类声明（ClassVar）：
    - mc_submodules: 需要的 MC 子模块（如 ("client","field","help")）
    - cdp_page_key: _cdp_browser 的 page 缓存键（douyin→"dy" 与平台名不同）
    - mc_lock = asyncio.Lock()（每平台一把，统一原 _client_lock/_page_lock/_creator_lock）

    客户端模板（douyin/xhs 使用；kuaishou 无 client 只走门户 + 锁 + page 设施）：
    ensure_client = 取页面 → 前置 hook → cookies → headers → 构造 client →
    登录检查 → 扫码引导 → 失败处置（子类 hook 决定 raise 或放行）。

    未来 MC 平台形态容纳（#3 Q5 设计校验）：weibo 数据走 httpx client、
    CDP 仅登录——其 client 构造仍需 page/cookie（update_cookies 同步登录态），
    模板同样适用；纯 HTTP 数据路径留在子类（不在此模板范围内）。
    """

    # -- 门户与共享设施 --

    mc_submodules: ClassVar[tuple[str, ...]] = ()
    # vendor media_platform 下的包名：与平台名可不一致（xiaohongshu→"xhs"）
    mc_package: ClassVar[str] = ""
    cdp_page_key: ClassVar[str] = ""
    # 页面 goto 就绪策略：douyin="commit"（B19 冷路径减负，就绪由轮询兜底），其余平台默认
    _page_wait_until: ClassVar[str] = "domcontentloaded"
    mc_lock: ClassVar[asyncio.Lock]

    @classmethod
    def import_mc(cls) -> dict[str, Any]:
        """导入本平台声明的 MC 子模块（缓存），返回 {submodule: module}。"""
        return import_mc_platform(cls.mc_package, *cls.mc_submodules)

    @classmethod
    def inject_config(cls) -> None:
        """注入本平台的平台键（在 client 构造前、锁内调用）。"""
        inject_platform_config(cls.name)

    @classmethod
    def get_page(cls, url: str) -> Any:
        """取本平台的 CDP 页面（_cdp_browser 缓存/失效重建）。"""
        return get_page_for_platform(cls.cdp_page_key, url, wait_until=cls._page_wait_until)

    @classmethod
    async def cdp_cookies(cls, page: Any) -> tuple[str, Any]:
        """把浏览器 context cookies 转成 (cookie 串, cookie 字典)。"""
        cookie_str, cookie_dict = await get_mc_utils().convert_browser_context_cookies(
            page.context, urls=list(cls._cookie_urls),
        )
        return cookie_str, cookie_dict

    @classmethod
    async def reset_client(cls) -> None:
        """重置客户端引用与页面缓存（不关闭 CDP 浏览器——那是用户的真实浏览器）。"""
        await invalidate_page(cls.cdp_page_key)
        cls._client = None
        cls._client_page = None

    @classmethod
    def run_on_cdp(cls, coro: Any) -> Any:
        """同步入口：提交协程到 CDP 专用常驻循环并阻塞等待（download() 用）。"""
        return run_on_cdp_loop(coro)

    @classmethod
    async def run_on_cdp_async(cls, coro: Any) -> Any:
        """异步入口：从 uvicorn 主循环提交协程到 CDP 循环（search 等用）。"""
        return await run_on_cdp_loop_async(coro)

    # -- 客户端生命周期（douyin/xhs 模板） --

    _client: ClassVar[Any] = None
    _client_page: ClassVar[Any] = None
    _client_cls: ClassVar[Any] = None
    _client_cls_name: ClassVar[str] = ""
    _client_timeout: ClassVar[float] = 30
    _home_url: ClassVar[str] = ""
    _cookie_urls: ClassVar[tuple[str, ...]] = ()

    @classmethod
    async def ensure_client(cls) -> Any:
        """MC client 懒构造模板：门户加载（配置注入+模块导入）→ 取页面 →
        前置 hook → cookies → headers → 构造 → 登录检查 → 扫码引导 → 失败处置。

        注意：模板内不加锁——调用方（如 douyin 的 _client_call）持 mc_lock
        调用本方法，内部再加锁会死锁。锁纪律保持在各平台调用点。
        """
        if cls._client is not None and cls._client_page is not None:
            try:
                if not cls._client_page.is_closed():
                    return cls._client
            except Exception:
                pass
            # page 已关闭 → 重建
            cls._client = None
            cls._client_page = None

        # 门户加载：配置注入（平台键唯一写入者）→ 模块导入 → 解析 client 类
        cls.inject_config()
        cls._client_cls = getattr(cls.import_mc()["client"], cls._client_cls_name)

        page = await cls._acquire_page()
        await cls._pre_client_hook(page)
        cookie_str, cookie_dict = await cls.cdp_cookies(page)
        headers = await cls._build_headers(page, cookie_str)

        cls._client = cls._client_cls(
            timeout=cls._client_timeout,
            proxy=cls._client_proxy(),
            headers=headers,
            playwright_page=page,
            cookie_dict=cookie_dict,
        )
        cls._client_page = page

        # 登录态检查：未登录 → 引导扫码；成功后刷新 cookie 到 client
        if not await cls._is_logged_in(page, cookie_dict):
            await cls._guide_login(page, cls._client)
            if await cls._is_logged_in(page, cookie_dict):
                await cls._client.update_cookies(
                    page.context, urls=list(cls._cookie_urls),
                )
            else:
                return await cls._handle_login_failure(page, cls._client)

        logger.info("%s client 已就绪 (CDP)", cls.name)
        return cls._client

    # -- 模板 hooks（子类实现平台差异） --

    @classmethod
    async def _acquire_page(cls) -> Any:
        """获取本平台 CDP 页面（含平台错误翻译）。"""
        raise NotImplementedError

    @classmethod
    async def _pre_client_hook(cls, page: Any) -> None:
        """构造 client 前的平台前置步骤（默认无）。"""

    @classmethod
    async def _build_headers(cls, page: Any, cookie_str: str) -> dict:
        """构造 client 请求头（含 UA/Cookie，风控放行的关键）。"""
        raise NotImplementedError

    @classmethod
    async def _is_logged_in(cls, page: Any, cookie_dict: dict) -> bool:
        """登录态判定（平台各自实现：localStorage/cookie/pong）。"""
        raise NotImplementedError

    @classmethod
    async def _guide_login(cls, page: Any, client: Any) -> None:
        """引导扫码登录（打开登录弹窗 + 轮询登录态）。"""
        raise NotImplementedError

    @classmethod
    async def _handle_login_failure(cls, page: Any, client: Any) -> Any:
        """引导失败处置：douyin 抛错（清 client），xhs 放行（继续未登录尝试）。"""
        raise NotImplementedError

    @classmethod
    def _client_proxy(cls) -> str | None:
        """client 代理（默认读 youtube_proxy——douyin/xhs 现状）。"""
        return settings.youtube_proxy or None
