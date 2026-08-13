"""VidAgent FastAPI 后端：SSE Relay + 工具 REST API。

启动：
  cd /home/lionel/Code/VidAgent
  uv run uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload

端点：
  POST /v1/chat/completions          SSE Relay → vLLM bare mode
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

from server.sse_relay import relay_stream, relay_stream_transparent

# 确保 vidagent 模块的 INFO 日志能输出到 uvicorn 控制台
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logging.getLogger("vidagent").setLevel(logging.INFO)

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
# 配置（provider 预设解析：vllm/siliconflow/generic）
# ---------------------------------------------------------------------------

from vidagent import llm_provider

# Agent 端点（relay 上游）+ provider 模式：由 provider 预设解析（vllm/siliconflow/generic）
VLLM_URL = llm_provider.agent_endpoint().base_url

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
    video_id: str | None = None
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
    """OpenAI 兼容的 chat completions 端点（SSE Relay）。

    AI SDK useChat 连接此端点。按 provider 预设分流：
    - vLLM-omni（xml 模式）：bare mode 无原生 function calling → 检测 <tool_call> XML → 转换为 tool_calls delta
    - 标准端点（transparent 模式）：原生 function calling → tools 原样转发、SSE 逐行透传
    """
    ep = llm_provider.agent_endpoint()
    if llm_provider.relay_mode() == "transparent":
        gen = relay_stream_transparent(
            request, upstream_url=ep.base_url, model=ep.model, api_key=ep.api_key
        )
    else:
        gen = relay_stream(request, vllm_url=ep.base_url, api_key=ep.api_key, model=ep.model)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
        # 平台明确不支持热榜（如小红书）：返回带提示的正常结果而非 400，
        # 让主 agent 从工具结果 message 中得知不支持并引导用户改用搜索
        return {"status": "ok", "results": [], "count": 0, "message": str(e)}
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

        last_summary = ""
        last_summary_stage = ""
        last_download_pct = -1
        last_chunks_snapshot = ""
        last_local_path_sent = False
        last_summary_active_flag = False

        while True:
            task = _summarize_tasks.get(task_id)
            if task is None:
                yield f"data: {json.dumps({'type': 'error', 'message': '任务不存在'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            if task["status"] in ("done", "error"):
                if task["status"] == "done":
                    yield f"data: {json.dumps({'type': 'done', 'result': task.get('result', ''), 'chapters': task.get('chapters', []), 'local_path': task.get('local_path', '')}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': task['result']}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # ★ 下载完成即推送 local_path（不等总结完成）
            if not last_local_path_sent and task.get("local_path"):
                last_local_path_sent = True
                yield f"data: {json.dumps({'type': 'progress', 'stage': 'downloaded', 'local_path': task['local_path']}, ensure_ascii=False)}\n\n"

            # 轮询 per-task progress
            progress = get_progress(task_id)
            if progress is not None:
                summary_active = progress.active
                summary_text = progress.partial if progress.active else ""
                # ★ 推送阶段事件：stage 变化 或 download_pct 变化（下载进度实时更新）
                stage = getattr(progress, 'stage', '') or ''
                download_pct = getattr(progress, 'download_pct', 0)
                if stage != last_summary_stage or download_pct != last_download_pct:
                    last_summary_stage = stage
                    last_download_pct = download_pct
                    yield f"data: {json.dumps({'type': 'progress', 'stage': stage, 'download_pct': download_pct}, ensure_ascii=False)}\n\n"

                # ★ 分块进度推送：chunks 内容变化时（长视频分段总结的逐段状态）
                chunks = getattr(progress, 'chunks', []) or []
                chunks_snapshot = json.dumps(chunks, ensure_ascii=False)
                if chunks_snapshot != last_chunks_snapshot:
                    last_chunks_snapshot = chunks_snapshot
                    yield f"data: {json.dumps({'type': 'progress', 'chunks': chunks}, ensure_ascii=False)}\n\n"

            if summary_active != last_summary_active_flag or summary_text != last_summary:
                last_summary_active_flag = summary_active
                last_summary = summary_text
                if summary_text:
                    # 纯文本事件：不带 stage，避免覆盖阶段事件设置的 task_status
                    yield f"data: {json.dumps({'type': 'progress', 'message': summary_text}, ensure_ascii=False)}\n\n"

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
    """按 video_id 获取总结进度 SSE（浏览器端 EventSource 使用）。

    特殊处理：SSE 可能在 POST /batch-summarize 之前到达（AI SDK 状态机时序），
    此时 _video_task_map 尚未注册。轮询等待最多 5 秒，而非立即 404。
    """
    # 轮询等待 mapping 注册（最多 5 秒，50ms 间隔）
    for _ in range(100):
        task_id = _video_task_map.get(video_id)
        if task_id:
            return await tool_summarize_stream(task_id)
        await asyncio.sleep(0.05)

    # 诊断：SSE 键与注册键不一致（前端 extractVideoId 与后端 _extract_video_id 差异）
    logger.warning(
        "by-video SSE 未命中映射: video_id=%r 已注册键=%s",
        video_id, sorted(_video_task_map.keys()),
    )
    raise HTTPException(status_code=404, detail="未找到该视频的总结任务（等待超时）")


# ---------------------------------------------------------------------------
# 批量总结工具（并行下载 + 总结多个视频）
# ---------------------------------------------------------------------------


def _extract_video_id(video_url: str) -> str | None:
    """从视频 URL 中提取平台原生 video_id。

    支持：B站 BV 号、YouTube video ID 等。
    """
    # 确保平台模块已注册
    import vidagent.tools.platforms.bilibili     # noqa: F401
    import vidagent.tools.platforms.youtube      # noqa: F401
    import vidagent.tools.platforms.douyin       # noqa: F401
    import vidagent.tools.platforms.kuaishou     # noqa: F401
    import vidagent.tools.platforms.xiaohongshu  # noqa: F401
    from vidagent.tools.platforms import detect_platform

    platform = detect_platform(video_url)
    if platform is not None:
        return platform.extract_video_id(video_url)
    # 回退：尝试 BV 正则
    import re
    m = re.search(r"(BV[\w]+)", video_url)
    return m.group(1) if m else None


@app.post("/api/tools/batch-summarize")
async def tool_batch_summarize(req: BatchSummarizeRequest):
    """批量并行总结：接受视频列表，后端并行处理下载+总结。

    每个视频独立：下载 → 抽取音频+帧 → Omni 多模态总结。
    各视频完全独立，一视频失败不影响其他。下载/总结各重试最多 5 次（指数退避）。

    返回：
        { batch_id, tasks: [{ task_id, video_id, status }] }
    前端可立即按 video_id 连接 SSE 获取各视频流式进度。
    """
    from vidagent.tools.summarizer import cleanup_progress, get_progress, create_progress
    from vidagent.tools.downloader import download_video

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    tasks: list[dict] = []

    MAX_RETRIES = 5
    RETRY_BASE_DELAY = 2  # 秒，指数退避：2s / 4s / 8s / 16s

    def _run_one(video: dict) -> None:
        task_id = video["_task_id"]
        video_id = video["_video_id"]

        try:
            # ── 提前创建 progress（下载阶段也需要推送进度）──
            pg = create_progress(task_id)
            pg.stage = "downloading"

            # ── 下载（重试）──
            local_path = None
            last_err = None
            video_url = video.get("video_url", "")

            # 下载进度回调（pg 已在 try 开头创建）
            # yt-dlp 分多流下载（video+audio），每条流独立报 0→100%
            # 因此进度只升不降，避免看到"满→空→满"的视觉回退
            def _dl_progress(pct: int) -> None:
                if pct > pg.download_pct:
                    pg.download_pct = pct

            for retry in range(1, MAX_RETRIES + 1):
                try:
                    result = download_video(video_url, video_id, progress_callback=_dl_progress)
                    if result.get("status") == "success":
                        local_path = result["local_path"]
                        _summarize_tasks[task_id]["local_path"] = local_path
                        break
                    last_err = result.get("error", "未知下载错误")
                    if result.get("fatal"):
                        # 确定性业务错误（如小红书图文笔记），重试无意义
                        break
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
                _summarize_tasks[task_id]["status"] = "error"
                _summarize_tasks[task_id]["result"] = f"下载失败(已重试{MAX_RETRIES}次): {last_err}"
                cleanup_progress(task_id)
                _video_task_map.pop(video_id, None)
                tasks[-1]["status"] = "error"
                return

            # ── 预处理：音频 + 均匀帧（Phase 1 立即启动）──
            from vidagent.utils.frames import extract_frames
            from vidagent.utils.audio import extract_audio
            from vidagent.tools.summarizer import _summarize_multimodal_with_chapters, _summarize_short_video

            # 获取已创建的 per-task progress（下载阶段已通过 create_progress 创建）
            pg = get_progress(task_id)
            pg.stage = "extracting"
            pg.begin()  # 激活流式输出 → SSE 端点开始推送

            video_path = Path(local_path)
            metadata = {
                "title": video.get("title", ""),
                "desc": video.get("desc", ""),
                "video_id": video_id,
                "author": video.get("author"),
                "duration_text": video.get("duration_text"),
                "duration": video.get("duration"),
            }

            # 提取音频（短视频和长视频都需要）
            mp3 = extract_audio(local_path)
            duration = metadata.get("duration") or 0
            # 补充缺失的 duration：从本地文件 ffprobe 获取（热搜/搜索结果常缺 duration）
            if not duration:
                from vidagent.utils.frames import get_duration as _get_dur
                duration = _get_dur(local_path)
                metadata["duration"] = duration
                metadata["duration_text"] = f"{int(duration // 60):02d}:{int(duration % 60):02d}"
                logger.info("📐 补充 duration: %.0fs", duration)

            _ep = llm_provider.multimodal_endpoint()
            base_url, api_key, model = _ep.base_url, _ep.api_key, _ep.model

            # ── 分流：短视频 vs 长视频 ──
            if isinstance(duration, (int, float)) and 0 < duration < 90:
                # ═══════ 短视频管线（<90s）═══════
                logger.info("🎬 短视频管线: %.0fs", duration)
                pg.stage = "summarizing"
                with _llm_semaphore:
                    summary = _summarize_short_video(
                        video_path=video_path,
                        metadata=metadata,
                        base_url=base_url, api_key=api_key, model=model,
                        progress=pg,
                    )
                chapters: list[dict] = []
            else:
                # ═══════ 长视频管线（≥90s）═══════
                # Phase 1: 均匀采样帧
                phase1_frames = extract_frames(video_path, duration=duration)

                def _do_phase1():
                    """Phase 1: 完整音频 + 均匀帧 → 流式总结"""
                    logger.info("📦 Phase 1 开始（%d 帧）…", len(phase1_frames))
                    chapters, summary = _summarize_multimodal_with_chapters(
                        mp3_path=Path(mp3),
                        metadata=metadata,
                        candidate_boundaries=[],  # 空 → 无章节模式（Phase 2 已随旧栈删除）
                        candidate_frames=phase1_frames,
                        base_url=base_url, api_key=api_key, model=model,
                        progress=pg,
                    )
                    return {"summary": summary, "chapters": chapters}

                phase1_result = _do_phase1()
                summary = phase1_result.get("summary", "")
                chapters: list[dict] = []

            _summarize_tasks[task_id]["status"] = "done"
            _summarize_tasks[task_id]["result"] = summary
            _summarize_tasks[task_id]["chapters"] = chapters
            tasks[-1]["status"] = "done"
            logger.info(
                "✅ 批量总结完成: %s (%s) | chapters=%d",
                video_id, video.get("title", ""), len(chapters),
            )

        except Exception as e:
            logger.exception("批量总结任务 %s 异常", video_id)
            _summarize_tasks[task_id]["status"] = "error"
            _summarize_tasks[task_id]["result"] = str(e)
            tasks[-1]["status"] = "error"
        finally:
            cleanup_progress(task_id)

    # 预注册所有 task（SSE 连接在 POST 请求期间就会到达，需提前建立映射）
    video_list = [v.model_dump() for v in req.videos]
    for video in video_list:
        task_id = uuid.uuid4().hex[:12]
        video_id = (
            video.get("video_id")
            or _extract_video_id(video.get("video_url", ""))
            or f"vid_{abs(hash(video.get('video_url') or video.get('title', task_id))) & 0xFFFFFFFF:08x}"
        )
        video["_task_id"] = task_id
        video["_video_id"] = video_id
        _video_task_map[video_id] = task_id
        _summarize_tasks[task_id] = {
            "status": "processing",
            "result": None,
            "partial": f"⏳ {video.get('title', video_id)} 排队中…",
        }
        tasks.append({"task_id": task_id, "video_id": video_id, "status": "processing"})

    # 所有视频并行提交到线程池
    futures = []
    for video in video_list:
        futures.append(_executor.submit(_run_one, video))

    logger.info("批量总结启动: batch=%s, %d 个视频，等待完成…", batch_id, len(req.videos))
    logger.info("批量总结注册键: %s", [v["_video_id"] for v in video_list])

    # ★ 关键：等待移到线程池，释放 asyncio 事件循环给 SSE 请求
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: [f.result() for f in futures])

    # 汇总结果
    # 构建 video_id → 原始请求数据 的映射（用于 title + video_url 回填）
    req_by_id: dict[str, dict] = {}
    for v in req.videos:
        rid = v.video_id or _extract_video_id(v.video_url or "")
        if rid:
            req_by_id[rid] = v.model_dump()
    results = []
    for t in tasks:
        task_data = _summarize_tasks.get(t["task_id"], {})
        video_id = t["video_id"]
        req_v = req_by_id.get(video_id, {})
        if task_data.get("status") == "done":
            results.append({
                "video_id": video_id,
                "video_url": req_v.get("video_url", ""),
                "title": req_v.get("title") or video_id,
                "status": "done",
                "summary": task_data.get("result", ""),
                "local_path": task_data.get("local_path", ""),
            })
        else:
            results.append({
                "video_id": video_id,
                "status": "error",
                "error": task_data.get("result", "未知错误"),
            })

    # 清理 video_id → task_id 映射
    for video in video_list:
        _video_task_map.pop(video["_video_id"], None)

    logger.info("批量总结完成: batch=%s, done=%d/%d", batch_id,
                sum(1 for r in results if r["status"] == "done"), len(results))
    return {"batch_id": batch_id, "results": results}


@app.post("/api/tools/retry-summarize")
async def tool_retry_summarize(req: BatchSummarizeRequest):
    """单视频重试（前端失败卡片「重试」按钮调用）。

    立即返回，后台线程执行与 batch-summarize 完全相同的逻辑
    （注册 video_id→task_id 映射后，前端通过 by-video SSE 读取进度）。
    复用 batch 端点保证行为一致；SSE 端点有 5s 轮询兜底，无注册竞态。
    """
    import threading

    video_ids = [v.video_id or _extract_video_id(v.video_url or "") for v in req.videos]
    logger.info("单视频重试请求: %s", video_ids)

    def _bg_run():
        try:
            asyncio.run(tool_batch_summarize(req))
        except Exception:
            logger.exception("单视频重试后台任务异常")

    threading.Thread(target=_bg_run, daemon=True, name="vidagent-retry").start()
    return {
        "status": "started",
        "video_ids": [vid for vid in video_ids if vid],
        "message": "重试任务已启动，请通过 by-video SSE 读取进度",
    }


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
