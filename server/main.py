"""VidAgent FastAPI 后端：SSE Relay + 工具 REST API。

启动：
  cd /home/lionel/Code/VidAgent
  uv run uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

端点：
  POST /v1/chat/completions          SSE Relay → vLLM bare mode
  GET  /api/tools/definitions        工具定义（AI SDK 初始化）
  GET  /api/tools/hot                热榜查询
  GET  /api/tools/search             关键词搜索
  GET  /api/tools/creator            创作者视频
  POST /api/tools/download           视频下载
  POST /api/tools/summarize          多模态总结（异步）
  GET  /api/tools/summarize/{id}/stream  总结进度 SSE
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# 确保项目根在 sys.path（server/ 从项目根启动）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.sse_relay import relay_stream
from server.tool_definitions import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="VidAgent Server", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务 — 让前端可直接播放 workspace 下的视频 / 关键帧
_workspace_dir = _PROJECT_ROOT / "workspace"
_workspace_dir.mkdir(parents=True, exist_ok=True)
app.mount("/workspace", StaticFiles(directory=str(_workspace_dir)), name="workspace")

# ---------------------------------------------------------------------------
# 配置（从环境变量 / .env 读取，与 src/vidagent/config.py 共用）
# ---------------------------------------------------------------------------

from vidagent.config import settings

VLLM_URL = os.getenv("VLLM_URL", "http://127.0.0.1:6006/v1")
VLLM_API_KEY = os.getenv("OPENAI_API_KEY", "not-needed")

# Thread pool for sync tools (downloader, summarizer)
# 5 workers 支持并行下载 + 总结
_executor = ThreadPoolExecutor(max_workers=5)

# vLLM 并发控制：避免多视频同时 Omni 推理导致显存/队列拥塞
_llm_semaphore = threading.BoundedSemaphore(2)

# 总结任务追踪：{task_id: {"status": str, "result": str, "partial": str}}
_summarize_tasks: dict[str, dict[str, Any]] = {}
# video_id → task_id 映射（供浏览器按视频 ID 连接 SSE）
_video_task_map: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    video_url: str
    file_name: str


class SummarizeRequest(BaseModel):
    local_path: str
    metadata: dict | None = None


class BatchVideoItem(BaseModel):
    video_url: str
    video_id: str
    title: str
    desc: str | None = None
    author: str | None = None
    duration_text: str | None = None
    platform: str | None = None


class BatchSummarizeRequest(BaseModel):
    videos: list[BatchVideoItem]


# ---------------------------------------------------------------------------
# SSE Relay —— 核心端点
# ---------------------------------------------------------------------------


@app.post("/v1/chat/completions")
async def chat_completions(request: dict):
    """OpenAI 兼容的 chat completions 端点。

    AI SDK useChat 连接此端点。内部做 SSE Relay：
    - 请求 → vLLM bare mode（stream=true，无 tool_choice）
    - 实时检测 <tool_call> XML → 转换为 OpenAI tool_calls delta
    - 纯文本 → 透明透传
    """
    return StreamingResponse(
        relay_stream(request, vllm_url=VLLM_URL, api_key=VLLM_API_KEY),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------


@app.get("/api/tools/definitions")
async def get_tool_definitions():
    """返回 OpenAI 兼容的工具定义列表，供 AI SDK 初始化。"""
    return TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# 检索工具（异步，直接调用 vidagent.tools.crawler）
# ---------------------------------------------------------------------------


@app.get("/api/tools/hot")
async def tool_get_hot_videos(
    platform: str = Query("bilibili", description="平台"),
    limit: int = Query(10, description="返回数量"),
    date_filter: str | None = Query(None, description="日期过滤: today"),
):
    """获取平台热门视频榜单。"""
    from vidagent.tools.crawler import get_hot_videos

    try:
        results = await get_hot_videos(platform=platform, limit=limit, date_filter=date_filter)
        return {"status": "ok", "results": results, "count": len(results)}
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("get_hot_videos 失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tools/search")
async def tool_search_videos(
    platform: str = Query("bilibili", description="平台"),
    keyword: str = Query(..., description="搜索关键词"),
    limit: int = Query(10, description="返回数量"),
    date_filter: str | None = Query(None, description="日期过滤: today"),
):
    """按关键词搜索视频。"""
    from vidagent.tools.crawler import search_videos

    try:
        results = await search_videos(
            platform=platform, keyword=keyword, limit=limit, date_filter=date_filter
        )
        return {"status": "ok", "results": results, "count": len(results)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("search_videos 失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tools/creator")
async def tool_get_creator_videos(
    platform: str = Query("bilibili", description="平台"),
    creator: str = Query(..., description="创作者昵称或 UID"),
    limit: int = Query(10, description="返回数量"),
    date_filter: str | None = Query(None, description="日期过滤: today"),
):
    """获取创作者视频列表。"""
    from vidagent.tools.crawler import get_creator_videos

    try:
        results = await get_creator_videos(
            platform=platform, creator=creator, limit=limit, date_filter=date_filter
        )
        return {"status": "ok", "results": results, "count": len(results)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("get_creator_videos 失败")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# 下载工具（同步，线程池执行）
# ---------------------------------------------------------------------------


@app.post("/api/tools/download")
async def tool_download_video(req: DownloadRequest):
    """下载视频到本地 workspace。"""
    from vidagent.tools.downloader import download_video

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _executor, download_video, req.video_url, req.file_name
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "下载失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_video 失败")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# 总结工具（异步：POST 创建任务 → GET stream 监听进度）
# ---------------------------------------------------------------------------


@app.post("/api/tools/summarize")
async def tool_summarize_start(req: SummarizeRequest):
    """启动多模态总结任务，返回 task_id + stream_url。

    总结在后台线程执行，通过 GET /api/tools/summarize/{task_id}/stream 获取实时进度。
    若 metadata 含 video_id，同时建立 video_id → task_id 映射，
    供浏览器通过 GET /api/tools/summarize/by-video/{video_id}/stream 连接。
    """
    from vidagent.tools.summarizer import extract_and_summarize, cleanup_progress

    task_id = uuid.uuid4().hex[:12]
    _summarize_tasks[task_id] = {
        "status": "processing",
        "result": None,
        "partial": "⏳ 总结任务已创建…",
    }

    # 建立 video_id → task_id 映射
    video_id = (req.metadata or {}).get("video_id")
    if video_id:
        _video_task_map[video_id] = task_id

    def _run():
        try:
            result = extract_and_summarize(req.local_path, req.metadata, task_id=task_id)
            _summarize_tasks[task_id]["status"] = "done"
            _summarize_tasks[task_id]["result"] = result
        except Exception as e:
            logger.exception("总结任务 %s 失败", task_id)
            _summarize_tasks[task_id]["status"] = "error"
            _summarize_tasks[task_id]["result"] = str(e)
        finally:
            cleanup_progress(task_id)
            if video_id:
                _video_task_map.pop(video_id, None)

    _executor.submit(_run)

    return {
        "task_id": task_id,
        "status": "processing",
        "stream_url": f"/api/tools/summarize/{task_id}/stream",
    }


@app.get("/api/tools/summarize/{task_id}/stream")
async def tool_summarize_stream(task_id: str):
    """SSE 进度流：实时推送总结进度（per-task 隔离）。"""
    from vidagent.tools.summarizer import get_progress

    async def _stream():
        import json

        last_partial = ""
        last_summary = ""
        last_asr_active = False
        last_summary_active_flag = False

        while True:
            task = _summarize_tasks.get(task_id)
            if task is None:
                yield f"data: {json.dumps({'type': 'error', 'message': '任务不存在'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            if task["status"] in ("done", "error"):
                if task["status"] == "done":
                    yield f"data: {json.dumps({'type': 'done', 'result': task['result']}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': task['result']}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 轮询 per-task progress（优先）或全局进度（回退）
            progress = get_progress(task_id)
            if progress is not None:
                asr_active = False
                asr_text = ""
                summary_active = progress.active
                summary_text = progress.partial if progress.active else ""
            else:
                # 回退：未传 task_id 的旧调用仍走全局单例
                from vidagent.tools.summarizer import (
                    live_partial, live_summary, live_summary_active, live_active,
                )
                asr_active = live_active()
                summary_active = live_summary_active()
                asr_text = live_partial()
                summary_text = live_summary()

            if asr_active != last_asr_active or asr_text != last_partial:
                last_asr_active = asr_active
                last_partial = asr_text
                if asr_text:
                    yield f"data: {json.dumps({'type': 'progress', 'stage': 'asr', 'message': asr_text}, ensure_ascii=False)}\n\n"

            if summary_active != last_summary_active_flag or summary_text != last_summary:
                last_summary_active_flag = summary_active
                last_summary = summary_text
                if summary_text:
                    yield f"data: {json.dumps({'type': 'progress', 'stage': 'summary', 'message': summary_text}, ensure_ascii=False)}\n\n"

            await asyncio.sleep(0.05)  # ~20fps，感知为逐 token 流式

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/tools/summarize/by-video/{video_id}/stream")
async def tool_summarize_stream_by_video(video_id: str):
    """按 video_id 获取总结进度 SSE（浏览器端 EventSource 使用）。"""
    task_id = _video_task_map.get(video_id)
    if not task_id:
        raise HTTPException(status_code=404, detail="未找到该视频的总结任务")

    return await tool_summarize_stream(task_id)


# ---------------------------------------------------------------------------
# 批量总结工具（并行下载 + 总结多个视频）
# ---------------------------------------------------------------------------


@app.post("/api/tools/batch-summarize")
async def tool_batch_summarize(req: BatchSummarizeRequest):
    """批量并行总结：接受视频列表，后端并行处理下载+总结。

    每个视频独立：下载 → 抽取音频+帧 → Omni 多模态总结。
    各视频完全独立，一视频失败不影响其他。下载/总结各重试最多 3 次（指数退避）。

    返回：
        { batch_id, tasks: [{ task_id, video_id, status }] }
    前端可立即按 video_id 连接 SSE 获取各视频流式进度。
    """
    from vidagent.tools.summarizer import extract_and_summarize, cleanup_progress
    from vidagent.tools.downloader import download_video

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    tasks: list[dict] = []

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2  # 秒，指数退避：2s / 4s / 8s

    def _run_one(video: dict) -> None:
        task_id = uuid.uuid4().hex[:12]
        video_id = video["video_id"]

        # 注册任务
        _video_task_map[video_id] = task_id
        _summarize_tasks[task_id] = {
            "status": "processing",
            "result": None,
            "partial": f"⏳ {video.get('title', video_id)} 排队中…",
        }
        tasks.append({"task_id": task_id, "video_id": video_id, "status": "processing"})

        try:
            # ── 下载（重试）──
            local_path = None
            last_err = None
            for retry in range(1, MAX_RETRIES + 1):
                try:
                    result = download_video(video["video_url"], video_id)
                    if result.get("status") == "success":
                        local_path = result["local_path"]
                        break
                    last_err = result.get("error", "未知下载错误")
                except Exception as e:
                    last_err = str(e)
                if retry < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY ** retry
                    logger.warning(
                        "下载重试 %d/%d (%.0fs 后退避): %s",
                        retry, MAX_RETRIES, delay, video_id,
                    )
                    time.sleep(delay)

            if not local_path:
                _summarize_tasks[task_id] = {
                    "status": "error",
                    "result": f"下载失败(已重试{MAX_RETRIES}次): {last_err}",
                }
                cleanup_progress(task_id)
                _video_task_map.pop(video_id, None)
                tasks[-1]["status"] = "error"
                return

            # ── 总结（重试，受 semaphore 控制并发）──
            metadata = {
                "title": video.get("title", ""),
                "desc": video.get("desc", ""),
                "video_id": video_id,
                "author": video.get("author"),
                "duration_text": video.get("duration_text"),
            }

            last_err = None
            with _llm_semaphore:
                for retry in range(1, MAX_RETRIES + 1):
                    try:
                        summary = extract_and_summarize(
                            local_path, metadata, task_id=task_id,
                        )
                        _summarize_tasks[task_id] = {
                            "status": "done",
                            "result": summary,
                        }
                        tasks[-1]["status"] = "done"
                        logger.info(
                            "批量总结完成: %s (%s)", video_id, video.get("title", "")
                        )
                        return
                    except Exception as e:
                        last_err = str(e)
                        if retry < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY ** retry
                            logger.warning(
                                "总结重试 %d/%d (%.0fs 后退避): %s — %s",
                                retry, MAX_RETRIES, delay, video_id, last_err[:100],
                            )
                            time.sleep(delay)

                # 重试耗尽
                _summarize_tasks[task_id] = {
                    "status": "error",
                    "result": f"总结失败(已重试{MAX_RETRIES}次): {last_err}",
                }
                tasks[-1]["status"] = "error"

        finally:
            cleanup_progress(task_id)
            _video_task_map.pop(video_id, None)

    # 所有视频并行提交到线程池
    for video in req.videos:
        _executor.submit(_run_one, video.model_dump())

    logger.info("批量总结启动: batch=%s, %d 个视频", batch_id, len(req.videos))
    return {"batch_id": batch_id, "tasks": tasks}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "vllm_url": VLLM_URL}


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
