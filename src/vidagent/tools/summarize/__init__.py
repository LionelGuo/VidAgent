"""总结域（#4 深模块包）——原单文件 vidagent.tools.summarizer 拆包而来。

公开面（server/main.py 的 import 面保持不变，仅路径改为本包）：
- 进度：ProgressStage / Progress / create_progress / get_progress / cleanup_progress
- 管线入口：extract_and_summarize

内部模块：progress / prompts / transport / multimodal / short_video /
chapters / pipeline。除公开面外均视为包内私有。
"""

from vidagent.tools.summarize.pipeline import extract_and_summarize
from vidagent.tools.summarize.progress import (
    Progress,
    ProgressStage,
    cleanup_progress,
    create_progress,
    get_progress,
)

__all__ = [
    "Progress",
    "ProgressStage",
    "cleanup_progress",
    "create_progress",
    "extract_and_summarize",
    "get_progress",
]
