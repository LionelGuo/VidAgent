"""工具定义生成：Python 工具签名为单一真相源，生成 OpenAI 兼容格式。

端点 GET /api/tools/definitions 返回此模块生成的列表，
供前端 AI SDK useChat 初始化时获取。
"""

from __future__ import annotations

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_hot_videos",
            "description": "获取平台综合热门视频榜单（最贴近「今日热榜」）。返回视频列表，每项含 video_id/title/desc/publish_time/duration/duration_text/video_url/platform/author/view_count。注意：抖音热榜返回的是热搜话题（is_trending_topic=true），含 search_keyword 字段，需先用 search_videos 搜索该关键词获取真实视频后再总结。Agent 可直接按时长/播放量筛选，无需下载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["bilibili", "youtube", "douyin", "kuaishou", "xiaohongshu"],
                        "description": "平台：bilibili / youtube / douyin / kuaishou / xiaohongshu",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "返回条数上限",
                    },
                    "date_filter": {
                        "type": "string",
                        "enum": ["today", None],
                        "default": None,
                        "description": "时间过滤：today 表示仅当日",
                    },
                },
                "required": ["platform"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_videos",
            "description": "按关键词搜索视频。返回视频列表，每项含 video_id/title/desc/publish_time/duration/duration_text/video_url/platform/author/view_count。",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["bilibili", "youtube", "douyin", "kuaishou", "xiaohongshu"],
                        "description": "平台：bilibili / youtube / douyin / kuaishou / xiaohongshu",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词（必填）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "返回条数上限",
                    },
                    "date_filter": {
                        "type": "string",
                        "enum": ["today", None],
                        "default": None,
                        "description": "时间过滤：today 表示仅当日",
                    },
                },
                "required": ["platform", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_creator_videos",
            "description": "获取指定创作者（UP 主）的视频列表。creator 可为昵称（如「老番茄」，自动解析为 UID）或数字 UID。返回视频列表，每项含 video_id/title/desc/publish_time/duration/duration_text/video_url/platform/author/view_count。",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["bilibili", "youtube", "douyin", "kuaishou", "xiaohongshu"],
                        "description": "平台：bilibili / youtube / douyin / kuaishou / xiaohongshu",
                    },
                    "creator": {
                        "type": "string",
                        "description": "创作者昵称或数字 UID（必填）",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "返回条数上限",
                    },
                    "date_filter": {
                        "type": "string",
                        "enum": ["today", None],
                        "default": None,
                        "description": "时间过滤：today 表示仅当日",
                    },
                },
                "required": ["platform", "creator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_video",
            "description": "下载无水印视频到本地。传入视频 URL（来自搜索/热榜结果的 video_url），下载到 workspace 目录。支持缓存复用：同一视频不重复下载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_url": {
                        "type": "string",
                        "description": "视频播放页地址（来自检索结果的 video_url 字段）",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "保存文件名前缀，通常使用 video_id",
                    },
                },
                "required": ["video_url", "file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_and_summarize",
            "description": "对本地视频生成结构化中文总结（Markdown）。核心能力：音频提取 → 多模态模型分析（音频+关键帧画面直送 LLM，无需 ASR 转写）→ 输出核心观点 + 主要内容梳理。支持最长 1 小时视频。无音频轨时自动降级为仅依据元数据的总结。",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "本地视频文件路径（来自 download_video 返回的 local_path）",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "视频元数据，至少含 title 与 desc。可选字段：video_id（用于缓存）",
                        "properties": {
                            "title": {"type": "string", "description": "视频标题"},
                            "desc": {"type": "string", "description": "视频简介"},
                            "video_id": {"type": "string", "description": "视频 ID（用于转写缓存）"},
                            "platform": {"type": "string", "description": "平台"},
                            "author": {"type": "string", "description": "作者"},
                            "duration_text": {"type": "string", "description": "时长文本"},
                        },
                    },
                },
                "required": ["local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_summarize_videos",
            "description": "【推荐】批量并行总结多个视频。后端自动并行下载+总结，比逐个调用 extract_and_summarize 更快。传入视频列表，后台同时处理所有视频（下载→多模态总结），每个视频独立重试、独立错误。总结结果在右侧详情面板实时流式显示。适用于用户要求同时总结多个视频的场景。注意：video_url 必须是具体视频的播放页地址（如 /video/xxx），不能是搜索页或话题页 URL。抖音热搜话题需先用 search_videos 搜索获取真实视频。",
            "parameters": {
                "type": "object",
                "properties": {
                    "videos": {
                        "type": "array",
                        "description": "要总结的视频列表。每项含 video_url 和 video_id（必填），以及 title/desc/author/duration_text（推荐，用于生成更精准的总结）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "video_url": {
                                    "type": "string",
                                    "description": "视频播放页地址（来自检索结果的 video_url 字段）",
                                },
                                "video_id": {
                                    "type": "string",
                                    "description": "视频 ID，如 BVxxx",
                                },
                                "title": {
                                    "type": "string",
                                    "description": "视频标题",
                                },
                                "desc": {
                                    "type": "string",
                                    "description": "视频简介",
                                },
                                "author": {
                                    "type": "string",
                                    "description": "作者/UP 主",
                                },
                                "duration_text": {
                                    "type": "string",
                                    "description": "时长文本，如 '12:34'",
                                },
                                "platform": {
                                    "type": "string",
                                    "description": "平台（默认从 URL 自动检测）",
                                },
                            },
                            "required": ["video_url", "video_id", "title"],
                        },
                    },
                },
                "required": ["videos"],
            },
        },
    },
]
