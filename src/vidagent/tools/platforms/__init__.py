"""平台抽象层：Platform 协议 + 注册表 + URL 检测。

每个平台模块导出一个 `Platform` 子类实例，具备统一接口：
- 检索：search / get_hot / get_creator（异步）
- 下载：download（同步，在线程池中调用）
- 工具：extract_video_id / normalize / make_client

注册表按 name 和 aliases 索引，get_platform() 按名称查找，
detect_platform() 从 URL 推断平台。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 统一 Schema（所有平台 normalize() 的输出格式）
# ---------------------------------------------------------------------------
# {
#     "video_id": str,        # 平台原生 ID
#     "title": str,
#     "desc": str,
#     "publish_time": int,    # unix timestamp
#     "duration": int,        # 秒
#     "duration_text": str,   # "MM:SS" 或 "H:MM:SS"
#     "video_url": str,       # 播放页 URL
#     "platform": str,        # "bilibili" | "youtube" | ...
#     "author": str,
#     "view_count": int,
# }


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
    def download(video_url: str, file_name: str) -> dict:
        """下载视频到本地。

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
            logger.warning("平台 %r 重复注册（%s），覆盖旧条目", n, platform.name)
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
    """从视频 URL 推测平台。

    Returns:
        Platform 实例，无法识别时返回 None。
    """
    u = url.lower()
    # Bilibili
    if "bilibili.com" in u or "b23.tv" in u:
        return _registry.get("bilibili")
    # YouTube
    if "youtube.com" in u or "youtu.be" in u:
        return _registry.get("youtube")
    # 抖音
    if "douyin.com" in u:
        return _registry.get("douyin")
    # 小红书 (must check before kuaishou since xhslink is not kuaishou)
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return _registry.get("xiaohongshu")
    # 快手
    if "kuaishou.com" in u or "chenzhongtech.com" in u:
        return _registry.get("kuaishou")
    return None


def list_platforms() -> list[str]:
    """返回所有已注册平台名称（去重）。"""
    return sorted(set(p.name for p in _registry.values()))
