"""Agent 调度层：Agno Agent + 系统提示 + 三大工具（自然语言驱动）。"""

from __future__ import annotations

from agno.agent import Agent

from vidagent.llm import build_model
from vidagent.tools.crawler import search_and_fetch_videos
from vidagent.tools.downloader import download_video
from vidagent.tools.summarizer import extract_and_summarize

SYSTEM_PROMPT = """你是 VidAgent，一个自媒体视频采集与总结助手，通过调用工具完成用户的自然语言指令。

【可用工具】
- search_and_fetch_videos(platform, task_type, target_id, date_filter, limit)：获取视频元数据。
  task_type ∈ {hot_board(平台综合热门), search(关键词搜索), user_homepage(创作者主页)}。
- download_video(video_url, file_name)：下载视频到本地，返回 local_path。
- extract_and_summarize(local_path, metadata)：对本地视频抽音转写并生成结构化总结。

【工作准则】
- 平台默认且仅支持 "bilibili"；用户未指定平台时按 bilibili 处理。
- 用户想「看/总结」视频时，按序调用：search_and_fetch_videos → download_video →
  extract_and_summarize。
  file_name 用 video_id；metadata 传 search 返回的该视频字典（含 title/desc）。
- 多个视频时逐个完成「下载→总结」，并用简短进度告知用户（如「正在处理 2/5…」）。
- 工具返回 status=error 或抛异常时：简要说明原因，最多重试 3 次；仍失败则如实告知，绝不编造内容。
- 全程中文；总结用 Markdown，分「核心观点」与「主要内容梳理」。
"""


def build_agent() -> Agent:
    return Agent(
        name="VidAgent",
        model=build_model(),
        tools=[search_and_fetch_videos, download_video, extract_and_summarize],
        instructions=SYSTEM_PROMPT,
        markdown=True,
        retries=3,
    )
