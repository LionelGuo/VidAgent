"""B站 API 封装 — 向后兼容 shim。

实际实现已移入 vidagent.tools.platforms.bilibili。
保留此模块是为了不破坏现有 import 路径（tests / crawler / hotboard）。
"""

from vidagent.tools.platforms.bilibili import (  # noqa: F401
    API_BASE,
    BiliAPIError,
    DEFAULT_HEADERS,
    _author_name,
    _check,
    _ensure_fingerprint,
    _fmt_duration,
    _get,
    _normalize_user,
    _parse_cookies,
    _parse_duration,
    _pick_best_user,
    _strip_html,
    _view_count,
    extract_video_id,
    fetch_popular,
    fetch_ranking,
    fetch_user_videos,
    make_client,
    normalize,
    resolve_creator_mid,
    search_users,
    search_videos,
)
