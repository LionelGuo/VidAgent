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

# 确保 vidagent 模块的 INFO 日志能输出到 uvicorn 控制台
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logging.getLogger("vidagent").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="VidAgent Server", version="0.2.0")

# ── 启动时预加载本地模型（避免首次请求冷启动延迟）──
@app.on_event("startup")
async def _preload_models():
    """预加载 TransNetV2 场景检测模型（本机 GPU）。"""
    try:
        from vidagent.utils.frames import _get_transnet
        import time
        t0 = time.perf_counter()
        _get_transnet()
        logger.info("✅ TransNetV2 预加载完成 (%.1fs)", time.perf_counter() - t0)
    except Exception as e:
        logger.warning("TransNetV2 预加载失败，将回退 ffmpeg: %s", e)

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

VLLM_URL = os.getenv("VLLM_URL") or settings.openai_base_url
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
        last_summary_stage = ""
        last_local_path_sent = False
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
                    yield f"data: {json.dumps({'type': 'done', 'result': task.get('result', ''), 'chapters': task.get('chapters', []), 'local_path': task.get('local_path', '')}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': task['result']}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # ★ 下载完成即推送 local_path（不等总结完成）
            if not last_local_path_sent and task.get("local_path"):
                last_local_path_sent = True
                yield f"data: {json.dumps({'type': 'progress', 'stage': 'downloaded', 'local_path': task['local_path']}, ensure_ascii=False)}\n\n"

            # 轮询 per-task progress（优先）或全局进度（回退）
            progress = get_progress(task_id)
            if progress is not None:
                asr_active = False
                asr_text = ""
                summary_active = progress.active
                summary_text = progress.partial if progress.active else ""
                # ★ 检测 stage 变化，推送阶段事件
                stage = getattr(progress, 'stage', '') or ''
                if stage != last_summary_stage:
                    last_summary_stage = stage
                    yield f"data: {json.dumps({'type': 'progress', 'stage': stage, 'download_pct': getattr(progress, 'download_pct', 0)}, ensure_ascii=False)}\n\n"
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
    各视频完全独立，一视频失败不影响其他。下载/总结各重试最多 3 次（指数退避）。

    返回：
        { batch_id, tasks: [{ task_id, video_id, status }] }
    前端可立即按 video_id 连接 SSE 获取各视频流式进度。
    """
    from vidagent.tools.summarizer import extract_and_summarize, cleanup_progress, get_progress
    from vidagent.tools.downloader import download_video

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    tasks: list[dict] = []

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2  # 秒，指数退避：2s / 4s / 8s

    def _run_one(video: dict) -> None:
        task_id = video["_task_id"]
        video_id = video["_video_id"]

        try:
            # ── 下载（重试）──
            local_path = None
            last_err = None
            video_url = video.get("video_url", "")
            # 更新进度 stage
            pg = get_progress(task_id)
            if pg:
                pg.stage = "downloading"

            for retry in range(1, MAX_RETRIES + 1):
                try:
                    result = download_video(video_url, video_id)
                    if result.get("status") == "success":
                        local_path = result["local_path"]
                        _summarize_tasks[task_id]["local_path"] = local_path
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
                _summarize_tasks[task_id]["status"] = "error"
                _summarize_tasks[task_id]["result"] = f"下载失败(已重试{MAX_RETRIES}次): {last_err}"
                cleanup_progress(task_id)
                _video_task_map.pop(video_id, None)
                tasks[-1]["status"] = "error"
                return

            # ── 预处理：音频 + 均匀帧（Phase 1 立即启动）──
            from vidagent.utils.frames import extract_frames, detect_boundaries
            from vidagent.utils.audio import extract_audio
            from vidagent.tools.summarizer import _summarize_multimodal_with_chapters, _match_chapters_segmented, _summarize_short_video, create_progress

            # 创建 per-task progress（替代 extract_and_summarize 中的 create_progress 调用）
            pg = create_progress(task_id)
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

            base_url = settings.multimodal_base_url or settings.openai_base_url
            api_key = settings.openai_api_key
            model = settings.multimodal_model or settings.llm_model

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
                # ═══════ 长视频管线（≥90s，现有逻辑不变）═══════
                # Phase 1: 均匀采样帧
                phase1_frames = extract_frames(video_path, duration=duration)

                # ── 并行：Phase 1 多模态总结 + 边界检测 ──
                import concurrent.futures as _cf

                phase1_result: dict = {"summary": "", "chapters": []}
                candidate_boundaries: list[int] = []
                candidate_frames: list[str] = []

                def _do_phase1():
                    """Phase 1: 完整音频 + 均匀帧 → 流式总结"""
                    logger.info("📦 Phase 1 开始（%d 帧）…", len(phase1_frames))
                    chapters, summary = _summarize_multimodal_with_chapters(
                        mp3_path=Path(mp3),
                        metadata=metadata,
                        candidate_boundaries=[],  # 空 → Phase 1 only (不触发 Phase 2)
                        candidate_frames=phase1_frames,
                        base_url=base_url, api_key=api_key, model=model,
                        progress=pg,
                    )
                    return {"summary": summary, "chapters": chapters}

                def _do_boundaries():
                    """后台：Silero VAD ∥ TransNetV2 场景检测 → 候选边界 + 中间帧"""
                    import subprocess as _sp

                    # ── 并行：Silero VAD（音频）+ 场景检测（视频），互相独立 ──
                    vad_times: list[float] = []
                    scene_times: set[float] = set()

                    def _run_vad() -> list[float]:
                        try:
                            import numpy as np
                            from faster_whisper.vad import (
                                SileroVADModel, get_speech_timestamps, VadOptions,
                            )
                            import faster_whisper

                            fw_dir = Path(faster_whisper.__file__).parent
                            vad_model = SileroVADModel(
                                str(fw_dir / "assets" / "silero_vad_v6.onnx")
                            )
                            raw = _sp.run(
                                ["ffmpeg", "-y", "-i", str(mp3), "-f", "s16le",
                                 "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", "-"],
                                capture_output=True, timeout=60,
                            )
                            if raw.returncode == 0 and raw.stdout:
                                audio = (np.frombuffer(raw.stdout, dtype=np.int16)
                                          .astype(np.float32) / 32768.0)
                                vad_opts = VadOptions(
                                    threshold=0.5, min_speech_duration_ms=500,
                                    min_silence_duration_ms=2000, max_speech_duration_s=120,
                                )
                                speech_ts = get_speech_timestamps(audio, vad_opts, sampling_rate=16000)
                                result = [s["start"] / 16000.0 for s in speech_ts]
                                logger.info("🔇 Silero VAD: %d 段", len(result))
                                return result
                        except Exception as e:
                            logger.warning("Silero VAD 失败: %s", e)
                            return []

                    def _run_scene_detect() -> list[tuple[float, float]] | None:
                        """TransNetV2 场景检测 → [(timestamp, probability), ...]"""
                        try:
                            from vidagent.utils.frames import _get_transnet
                            import tempfile
                            lowres = Path(tempfile.mktemp(suffix=".mp4"))
                            _sp.run(
                                ["ffmpeg", "-y", "-an", "-i", str(video_path),
                                 "-vf", "scale=320:-1,fps=4", "-preset", "ultrafast",
                                 "-crf", "28", "-c:v", "libx264", str(lowres)],
                                capture_output=True, timeout=max(30, int(metadata.get("duration", 30))),
                            )
                            model = _get_transnet()
                            scenes = model.detect_scenes(str(lowres))
                            lowres.unlink(missing_ok=True)
                            result = [
                                (float(s["start_time"]), s["probability"])
                                for s in scenes
                                if 0 < float(s["start_time"]) < metadata.get("duration", float("inf"))
                            ]
                            logger.info("🎬 TransNetV2: %d 个场景边界", len(result))
                            return result
                        except Exception as e:
                            logger.warning("TransNetV2 失败: %s", e)
                            return None

                    # 并行执行 VAD + 场景检测
                    with _cf.ThreadPoolExecutor(max_workers=2) as _vpool:
                        _fv = _vpool.submit(_run_vad)
                        _fs = _vpool.submit(_run_scene_detect)
                        vad_times = _fv.result()
                        scene_probs = _fs.result()

                    # ── 动态阈值：取满足 ≤ 8/min 的最低阈值（最多候选点）──
                    video_minutes = (metadata.get("duration") or 60) / 60
                    scene_times: set[float] = set()
                    if scene_probs is not None:
                        chosen = 0.70
                        for threshold in [0.95, 0.90, 0.85, 0.80, 0.75, 0.70]:
                            filtered = {t for t, p in scene_probs if p > threshold}
                            if len(filtered) / max(video_minutes, 0.1) <= 8:
                                scene_times = filtered
                                chosen = threshold
                            else:
                                break  # 更高阈值已超限
                        logger.info(
                            "🎬 动态阈值: >%.2f → %d 个场景边界 (%.1f/min)",
                            chosen, len(scene_times),
                            len(scene_times) / max(video_minutes, 0.1),
                        )
                    else:
                        scene_times = None  # 让 detect_boundaries 内部回退

                    # ── 合并 VAD + 场景边界 → detect_boundaries ──
                    boundaries = detect_boundaries(
                        local_path,
                        vad_boundaries=vad_times or None,
                        scene_boundaries=scene_times if scene_times is not None else None,
                    )
                    if len(boundaries) >= 3:
                        mid_ts = [
                            (boundaries[i] + boundaries[i + 1]) / 2
                            for i in range(len(boundaries) - 1)
                        ]
                        frames = extract_frames(
                            video_path, timestamps=[float(t) for t in mid_ts],
                            output_dir=video_path.parent / f"keyframes_{video_id}_chapters",
                        )
                        return boundaries, [str(f) for f in frames]
                    return boundaries, []

                with _cf.ThreadPoolExecutor(max_workers=2) as _pool:
                    _f1 = _pool.submit(_do_phase1)
                    _f2 = _pool.submit(_do_boundaries)
                    phase1_result = _f1.result()
                    candidate_boundaries, candidate_frames = _f2.result()

                logger.info(
                    "📐 边界检测完成: %d 边界 → %s",
                    len(candidate_boundaries), candidate_boundaries,
                )

                # ── 结果处理 ──
                summary = phase1_result.get("summary", "")
                chapters = phase1_result.get("chapters", [])

                # Phase 2: 分段多模态匹配（有边界 + Phase 1 无章节时触发）
                if not chapters and len(candidate_boundaries) >= 3:
                    logger.info("📑 Phase 2: 分段匹配 (%d 段) …", len(candidate_boundaries) - 1)
                    with _llm_semaphore:
                        chapters = _match_chapters_segmented(
                            phase1_summary=summary,
                            mp3_path=Path(mp3),
                            candidate_boundaries=candidate_boundaries,
                            candidate_frames=[Path(f) for f in candidate_frames] if candidate_frames else [],
                            base_url=base_url, api_key=api_key, model=model,
                        )
                    # 重试一次
                    if not chapters:
                        logger.warning("Phase 2 首次失败，重试…")
                        with _llm_semaphore:
                            chapters = _match_chapters_segmented(
                                phase1_summary=summary,
                                mp3_path=Path(mp3),
                                candidate_boundaries=candidate_boundaries,
                                candidate_frames=[Path(f) for f in candidate_frames] if candidate_frames else [],
                                base_url=base_url, api_key=api_key, model=model,
                            )

                # 兜底
                if not chapters and len(candidate_boundaries) >= 3:
                    from vidagent.tools.summarizer import _fallback_chapters
                    chapters = _fallback_chapters(candidate_boundaries, int(metadata.get("duration") or 0))

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

    # ★ 关键：等待移到线程池，释放 asyncio 事件循环给 SSE 请求
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: [f.result() for f in futures])

    # 汇总结果
    results = []
    for t in tasks:
        task_data = _summarize_tasks.get(t["task_id"], {})
        video_id = t["video_id"]
        if task_data.get("status") == "done":
            results.append({
                "video_id": video_id,
                "title": next((v.title for v in req.videos if _extract_video_id(v.video_url) == video_id or v.video_id == video_id), video_id),
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
