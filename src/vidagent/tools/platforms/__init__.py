"""平台抽象层：Platform 协议 + 注册表 + URL 检测。

每个平台模块导出一个 `Platform` 子类实例，具备统一接口：
- 检索：search / get_hot / get_creator（异步）
- 下载：download（同步，在线程池中调用）
- 工具：extract_video_id / normalize / make_client
- url_patterns：平台 URL 域名模式（小写子串），detect_platform() 据此推断

注册表按 name 和 aliases 索引，get_platform() 按名称查找，
detect_platform() 从 URL 推断平台（遍历注册表匹配 url_patterns——
新增平台只需声明模式，无需改检测分支）。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 统一 Schema（所有平台 normalize() 的输出格式）
# ---------------------------------------------------------------------------
# 字段清单的单一来源：scripts/gen-tool-schema.py 提取本常量生成前端
# 工具 describe 的字段列表文本
VIDEO_FIELDS: tuple[str, ...] = (
    "video_id",  # 平台原生 ID
    "title",
    "desc",
    "publish_time",  # unix timestamp
    "duration",  # 秒
    "duration_text",  # "MM:SS" 或 "H:MM:SS"
    "video_url",  # 播放页 URL
    "platform",  # "bilibili" | "youtube" | ...
    "author",
    "view_count",
)


# ---------------------------------------------------------------------------
# Platform 基类
# ---------------------------------------------------------------------------

class Platform:
    """平台基类：子类覆盖 name / aliases + 实现检索 + 下载方法。

    检索方法默认抛 NotImplementedError（平台可能不支持该能力，如抖音无公开热榜）。
    下载方法必须实现。
    """

    name: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()
    # 平台 URL 域名模式（小写子串，如 "bilibili.com" / "b23.tv"）：
    # detect_platform 遍历注册表按此匹配，新平台在此声明即可
    url_patterns: ClassVar[tuple[str, ...]] = ()

    # 能力声明（单一来源：scripts/gen-tool-schema.py 提取生成前端工具
    # describe 的平台句）。子类按实测能力显式覆盖；capability_notes 按
    # 能力名附加使用条件（如 youtube 创作者查询需 API key）。
    supports_hot: ClassVar[bool] = False
    supports_search: ClassVar[bool] = False
    supports_creator: ClassVar[bool] = False
    capability_notes: ClassVar[dict[str, str]] = {}

    # -- 工具方法（子类必须实现） --

    @staticmethod
    def extract_video_id(url: str) -> str | None:
        """从 URL 提取平台原生 video_id，如 BVxxx / yt_xxx。"""
        raise NotImplementedError

    @staticmethod
    def normalize(raw: dict) -> dict:
        """平台原始 item → 统一 schema。"""
        raise NotImplementedError

    @staticmethod
    def make_client(**kwargs: Any) -> Any:  # → httpx.AsyncClient
        """创建平台 HTTP 客户端（异步）。"""
        raise NotImplementedError

    # -- 检索（子类按需覆盖） --

    @staticmethod
    async def search(client: Any, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索视频。"""
        raise NotImplementedError(f"{client} 平台暂不支持搜索")

    @staticmethod
    async def get_hot(client: Any, limit: int = 10) -> list[dict]:
        """获取热门/榜单视频。"""
        raise NotImplementedError(f"{client} 平台暂不支持热榜")

    @staticmethod
    async def get_creator(client: Any, creator: str, limit: int = 10) -> list[dict]:
        """获取创作者视频列表。creator 可为昵称或 ID。"""
        raise NotImplementedError(f"{client} 平台暂不支持创作者查询")

    # -- 下载（子类必须实现） --

    @staticmethod
    def download(video_url: str, file_name: str,
                 progress_callback: Callable[[int], None] | None = None) -> dict:
        """下载视频到本地。

        Args:
            video_url: 视频播放页地址。
            file_name: 保存文件名前缀。
            progress_callback: 下载进度回调，参数为 0-100 的百分比整数。

        Returns:
            {"status":"success","local_path":...,"platform":...,"cached":bool}
            或 {"status":"error","error":...,"video_url":...}
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 平台注册表
# ---------------------------------------------------------------------------

_registry: dict[str, Platform] = {}


def register(platform: Platform) -> Platform:
    """注册平台实例：按 name 和 aliases 索引。"""
    names = [platform.name, *platform.aliases]
    for n in names:
        n_lower = n.lower()
        if n_lower in _registry:
            logger.warning("平台 %r 重复注册(%s),覆盖旧条目", n, platform.name)
        _registry[n_lower] = platform
    logger.info("已注册平台: %s (别名: %s)", platform.name, ", ".join(platform.aliases))
    return platform


def get_platform(name: str) -> Platform:
    """按名称或别名获取平台实例。

    Raises:
        NotImplementedError: 平台未接入。
    """
    p = _registry.get(name.lower())
    if p is not None:
        return p
    available = sorted(set(p.name for p in _registry.values()))
    raise NotImplementedError(
        f"平台暂未接入: {name}（当前可用: {', '.join(available) or '无'}）"
    )


def detect_platform(url: str) -> Platform | None:
    """从视频 URL 推测平台（遍历注册表匹配 url_patterns 声明）。

    Returns:
        Platform 实例，无法识别时返回 None。
    """
    u = url.lower()
    # dict.fromkeys 去重：注册表按 name+aliases 多键索引同一实例
    for platform in dict.fromkeys(_registry.values()):
        if any(pattern in u for pattern in platform.url_patterns):
            return platform
    return None


def list_platforms() -> list[str]:
    """返回所有已注册平台名称（去重）。"""
    return sorted(set(p.name for p in _registry.values()))


# ---------------------------------------------------------------------------
# 平台模块清单（单一来源）
# ---------------------------------------------------------------------------
# 新增平台时在此加一行；crawler / downloader / server 的「确保已注册」
# 共用 ensure_platforms_imported()（#3 Q6：原三处重复的 5 平台 import 列表）

PLATFORM_MODULES: tuple[str, ...] = (
    "vidagent.tools.platforms.bilibili",
    "vidagent.tools.platforms.douyin",
    "vidagent.tools.platforms.kuaishou",
    "vidagent.tools.platforms.xiaohongshu",
    "vidagent.tools.platforms.youtube",
)

_platforms_loaded = False


def ensure_platforms_imported() -> None:
    """确保所有平台模块已加载并注册（幂等；模块导入触发 register()）。"""
    global _platforms_loaded
    if _platforms_loaded:
        return
    for module_name in PLATFORM_MODULES:
        importlib.import_module(module_name)
    _platforms_loaded = True
