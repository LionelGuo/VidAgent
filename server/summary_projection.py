"""总结进度 → SSE 事件的纯投影器（#4 深模块）。

把「任务记录 + 进度」投影为 wire 事件序列：终态 done/error、下载完成推送、
阶段变化、分块快照、流式文本增量。哨兵去重逻辑全内聚于此，server/main.py
的 _stream 轮询循环只负责「取快照 → 投影 → yield」。

纯函数约定：无全局依赖；`state` 原地更新（由单个轮询循环持有，无共享）。
与 #4 前的 main.py 内嵌投影逐字节等价——wire 契约不变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from server.models import TaskRecord, TaskStatus
from vidagent.tools.summarize import Progress, ProgressStage


@dataclass
class ProjectionState:
    """投影去重哨兵（跨轮询迭代的持久态）。"""

    last_summary: str = ""
    last_summary_stage: str = ""
    last_download_pct: int = -1
    last_chunks_snapshot: str = ""
    local_path_sent: bool = False
    last_summary_active_flag: bool = False


def project(
    state: ProjectionState,
    task: TaskRecord | None,
    progress: Progress | None,
) -> tuple[list[dict], bool]:
    """把一次轮询快照投影为 wire 事件列表。

    返回 (events, finished)：finished=True 表示任务终态（或不存在），
    调用方应发射 events 后结束流（[DONE] 标记由传输层负责）。
    """

    events: list[dict] = []

    if task is None:
        events.append({"type": "error", "message": "任务不存在"})
        return events, True

    if task.status is TaskStatus.DONE:
        events.append({
            "type": "done",
            "result": task.result or "",
            "chapters": task.chapters,
            "local_path": task.local_path or "",
        })
        return events, True

    if task.status is TaskStatus.ERROR:
        events.append({"type": "error", "message": task.result})
        return events, True

    # ★ 下载完成即推送 local_path（不等总结完成）
    if not state.local_path_sent and task.local_path:
        state.local_path_sent = True
        events.append({
            "type": "progress",
            "stage": ProgressStage.DOWNLOADED,
            "local_path": task.local_path,
        })

    if progress is not None:
        summary_active = progress.active
        summary_text = progress.partial if progress.active else ""

        # ★ 阶段事件：stage 变化 或 download_pct 变化（下载进度实时更新）
        stage = progress.stage or ""
        download_pct = progress.download_pct
        if stage != state.last_summary_stage or download_pct != state.last_download_pct:
            state.last_summary_stage = stage
            state.last_download_pct = download_pct
            events.append({
                "type": "progress",
                "stage": stage,
                "download_pct": download_pct,
            })

        # ★ 分块进度：chunks 快照变化时（长视频分段总结的逐段状态）
        chunks = progress.chunks
        chunks_snapshot = json.dumps(chunks, ensure_ascii=False)
        if chunks_snapshot != state.last_chunks_snapshot:
            state.last_chunks_snapshot = chunks_snapshot
            events.append({"type": "progress", "chunks": chunks})

        # ★ 文本增量：活跃标志或文本变化 → 更新哨兵；非空文本才发射
        #（纯文本事件不带 stage，避免覆盖阶段事件设置的 task_status）
        if summary_active != state.last_summary_active_flag or summary_text != state.last_summary:
            state.last_summary_active_flag = summary_active
            state.last_summary = summary_text
            if summary_text:
                events.append({"type": "progress", "message": summary_text})

    return events, False
