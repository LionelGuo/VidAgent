"""总结进度模型与 per-task 注册表（#4 深模块：自原 summarizer.py 拆出）。

ProgressStage 是总结进度阶段词汇表的后端唯一来源（前端 sse-events.ts
由 scripts/gen-sse-types.py 从此处生成）。Progress 是单视频总结执行中的
实时细节（CONTEXT.md「进度」词条），与 server.models.TaskRecord 分属两层。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProgressStage(StrEnum):
    """总结进度阶段词汇表（后端唯一来源；前端 stores.ts 的镜像校准归 #1）。

    DOWNLOADED 由 SSE 端点在 local_path 就绪时合成，从不写入 Progress.stage；
    空串 "" 是空闲哨兵（初始 / reset 态），不是阶段成员——发射新值会改变前端可见行为。
    """

    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    EXTRACTING = "extracting"
    SUMMARIZING = "summarizing"
    THINKING = "thinking"
    SUMMARY = "summary"
    CHUNKING = "chunking"
    MERGING = "merging"


class Progress(BaseModel):
    """多模态总结实时流：SSE 轮询时获取当前已生成的总结文本（CONTEXT.md「进度」词条）。

    单用户 GIL 设计；工具线程写、SSE 协程读，GIL 下单属性读写原子，够用。
    支持两种模式：
    - streaming: 逐 token 追加（短音频单请求）
    - chunked: 逐段追加完整摘要（长音频分块）
    """

    model_config = ConfigDict(validate_assignment=True)

    active: bool = False
    partial: str = ""
    # 当前阶段（ProgressStage）；"" 为空闲哨兵（初始 / reset 态）
    stage: ProgressStage | Literal[""] = ""
    download_pct: int = 0  # 下载进度 0-100
    # 分块进度（长视频分段总结）：每段一条 {index, total, time_start, time_end, status, text}
    chunks: list[dict] = Field(default_factory=list)
    current_chunk: int = -1  # 当前流式写入的分块索引（-1 = 主输出 partial）

    def begin(self, label: str = "") -> None:
        self.active = True
        self.partial = label
        self.stage = ProgressStage.SUMMARIZING

    def append(self, text: str) -> None:
        self.partial += text

    def set(self, text: str) -> None:
        """替换全部（用于 chunk 进度更新）。"""
        self.partial = text

    def reset(self) -> None:
        self.active = False
        self.partial = ""
        self.stage = ""


# per-task progress（支持并行总结 + 前端流式）
_task_progress: dict[str, Progress] = {}


def create_progress(task_id: str) -> Progress:
    """创建一个 per-task 进度追踪器，存入全局 dict。"""
    tp = Progress()
    _task_progress[task_id] = tp
    return tp


def get_progress(task_id: str) -> Progress | None:
    """获取 per-task 进度追踪器。"""
    return _task_progress.get(task_id)


def cleanup_progress(task_id: str) -> None:
    """清理 per-task 进度追踪器。"""
    _task_progress.pop(task_id, None)
