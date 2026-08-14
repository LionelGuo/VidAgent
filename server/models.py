"""任务记录类型（#5 TaskRegistry typing）。

TaskRecord 是总结任务的服务器侧终态记录（CONTEXT.md「任务记录 (Task Record)」词条）：
服务器按 task_id 跟踪，SSE 端点轮询其状态产出 done/error 终态事件。
与「进度 (Progress)」（vidagent.tools.summarize，库内实时流式细节）分属两层，
见 CONTEXT.md 的 Task / Progress 词条。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    """总结任务终态：processing → done | error。"""

    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


class TaskRecord(BaseModel):
    """一次总结任务的服务器侧记录（替代此前的 stringly-typed dict）。

    全字段前置声明：旧实现中途加键（下载完成补 local_path、完成时补 chapters），
    现统一为创建即全字段 + 属性赋值。
    """

    model_config = ConfigDict(validate_assignment=True)

    status: TaskStatus
    result: str | None = None
    local_path: str | None = None
    chapters: list[dict] = Field(default_factory=list)

    def fail(self, message: str) -> None:
        """记录失败：状态置 ERROR，result 承载错误信息（沿用旧 dict 的字段语义）。"""
        self.status = TaskStatus.ERROR
        self.result = message
