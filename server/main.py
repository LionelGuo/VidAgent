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
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path

# 确保项目根在 sys.path（server/ 从项目根启动）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.models import TaskRecord, TaskStatus
from server.sse_relay import relay_stream, relay_stream_transparent
from vidagent.utils.logging import setup_logging

# 统一日志格式（模板 + 短标识符），须在其它模块输出日志前配置
setup_logging()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 启动预热（B19：douyin 冷调用 ~1min → 把一次性成本挪到启动期，不阻塞启动）
# ---------------------------------------------------------------------------


async def _warm_douyin() -> None:
    """后台预热抖音（预编译签名 JS + 预连 CDP），失败静默、不阻塞启动。"""
    try:
        from vidagent.tools.platforms.douyin import warm_startup

        await warm_startup()
    except Exception:
        logger.warning("抖音预热失败(不影响启动,首次调用时走冷路径)", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    warm_task = asyncio.create_task(_warm_douyin())
    try:
        yield
    finally:
        warm_task.cancel()
        with suppress(asyncio.CancelledError):
            await warm_task


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="VidAgent Server", version="0.2.0", lifespan=lifespan)

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
from vidagent.config import settings

# 启动校验：缺必填配置（vllm 缺 base_url/model、其余缺 key）时快速失败并给中文提示
llm_provider.validate_required()

# Agent 端点（relay 上游）+ provider 模式：由 provider 预设解析（vllm/siliconflow/generic）
VLLM_URL = llm_provider.agent_endpoint().base_url

# Thread pool for sync tools (downloader, summarizer)
# TASK_POOL_SIZE env-tunable（默认 8）：并行下载 + 预处理的工人数；
# LLM 推理并发不在此层，另有全局闸 ≤2（summarize/transport.py）
_executor = ThreadPoolExecutor(max_workers=settings.task_pool_size)

# 总结任务追踪（TaskRecord 类型化记录，字段见 server/models.py）
_summarize_tasks: dict[str, TaskRecord] = {}
# video_id → task_id 映射（供浏览器按视频 ID 连接 SSE）
_video_task_map: dict[str, str] = {}

# 工具 API 默认值（单一来源：scripts/gen-tool-schema.py 提取生成前端 zod default）
DEFAULT_PLATFORM = "bilibili"
DEFAULT_LIMIT = 10

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
    # 分P选择（B站）：用户已选定分P，服务端按 pagelist 展开为 per-P 条目
    # （?p=N URL / BVxxx-pN 键 /「主标题 · PN 子标题」全服务端生成，
    # 模型只传 BV 号 + P 号数组，不转写标题/URL）。上限 100 与前端 zod
    # 同步（防「全部」展开千P任务洪泛，提示词 >10P 已劝阻）
    parts: list[int] | None = Field(default=None, max_length=100)


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
    platform: str = Query(DEFAULT_PLATFORM, description="平台"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=50, description="返回数量"),
):
    """获取平台热门视频榜单（B11：热榜为实时榜单，无按发布日期过滤参数）。"""
    from vidagent.tools.crawler import get_hot_videos

    try:
        results = await get_hot_videos(platform=platform, limit=limit)
        return {"status": "ok", "results": results, "count": len(results)}
    except NotImplementedError as e:
        # 平台明确不支持热榜（如小红书）：返回带提示的正常结果而非 400，
        # 让主 agent 从工具结果 message 中得知不支持并引导用户改用搜索
        return {"status": "ok", "results": [], "count": 0, "message": str(e)}
    except Exception as e:
        logger.exception("get_hot_videos 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tools/search")
async def tool_search_videos(
    platform: str = Query(DEFAULT_PLATFORM, description="平台"),
    keyword: str = Query(..., description="搜索关键词"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=50, description="返回数量"),
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
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("search_videos 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tools/creator")
async def tool_get_creator_videos(
    platform: str = Query(DEFAULT_PLATFORM, description="平台"),
    creator: str = Query(..., description="创作者昵称或 UID"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=50, description="返回数量"),
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
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotImplementedError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("get_creator_videos 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# 下载工具（同步，线程池执行）
# ---------------------------------------------------------------------------


@app.post("/api/tools/download")
async def tool_download_video(req: DownloadRequest):
    """下载视频到本地 workspace。"""
    from vidagent.tools.downloader import download_video

    # B站分P检测：裸 BV 的多P视频不下载，返回分P清单引导模型询问用户（秒回）
    multi = await _detect_bili_multi_part(req.video_url)
    if multi:
        from vidagent.tools.platforms.bilibili import PARTS_LIST_CAP, part_url

        multi["status"] = "multi_part"
        multi["video_url"] = f"https://www.bilibili.com/video/{multi['bvid']}"
        # 清单/条目上送截断（与 batch 入口同口径；total_parts 仍是真实总数）
        multi["parts"] = multi["parts"][:PARTS_LIST_CAP]
        multi["entries"] = [
            {
                "video_url": part_url(multi["bvid"], p["page"]),
                "file_name": f"{multi['bvid']}-p{p['page']}",
            }
            for p in multi["parts"]
        ]
        multi.pop("bvid")
        multi["message"] = (
            f"该视频是分P视频（共 {multi['total_parts']} 个分P），未指定分P，未下载。"
            "请告知用户这是分P视频并询问要处理哪一P或全部；"
            "确定后按 entries 中对应条目的 video_url 与 file_name 重新调用下载。"
        )
        return multi

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
        raise HTTPException(status_code=500, detail=str(e)) from e


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
    from vidagent.tools.summarize import cleanup_progress, extract_and_summarize

    task_id = uuid.uuid4().hex[:12]
    _summarize_tasks[task_id] = TaskRecord(status=TaskStatus.PROCESSING)

    # 建立 video_id → task_id 映射
    video_id = (req.metadata or {}).get("video_id")
    if video_id:
        _video_task_map[video_id] = task_id

    def _run():
        try:
            result = extract_and_summarize(req.local_path, req.metadata, task_id=task_id)
            _summarize_tasks[task_id].status = TaskStatus.DONE
            _summarize_tasks[task_id].result = result
        except Exception as e:
            logger.exception("总结任务 %s 失败", task_id)
            _summarize_tasks[task_id].fail(str(e))
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
    """SSE 进度流：实时推送总结进度（per-task 隔离）。

    投影逻辑（哨兵去重）已内聚到 server.summary_projection.project（#4），
    本端点只负责「取快照 → 投影 → yield」。
    """
    from server.summary_projection import ProjectionState, project
    from vidagent.tools.summarize import get_progress

    async def _stream():
        import json

        state = ProjectionState()

        while True:
            task = _summarize_tasks.get(task_id)
            events, finished = project(state, task, get_progress(task_id))
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if finished:
                yield "data: [DONE]\n\n"
                return
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

    支持：B站 BV 号（带 ?p=N 时为 BVxxx-pN）、YouTube video ID 等。
    """
    # 确保平台模块已注册（清单单一来源，见 platforms/__init__.py）
    from vidagent.tools.platforms import detect_platform, ensure_platforms_imported

    ensure_platforms_imported()

    platform = detect_platform(video_url)
    if platform is not None:
        return platform.extract_video_id(video_url)
    # 回退：尝试 BV 正则
    import re
    m = re.search(r"(BV[\w]+)", video_url)
    return m.group(1) if m else None


async def _detect_bili_multi_part(video_url: str) -> dict | None:
    """B站分P检测（batch-summarize 与 download 两入口共用）。

    返回 None = 非分P场景（非 B站 URL / 已带 ?p= / 检测失败降级单P），
    调用方按原流程处理；返回 dict = 分P视频（bvid/parts/total_parts），
    调用方据此组装 multi_part 引导结构（不下载、秒回）。
    """
    from vidagent.tools.platforms import bilibili

    bvid, parts = await bilibili.detect_parts(video_url)
    if not parts:
        return None
    return {"bvid": bvid, "parts": parts, "total_parts": len(parts)}


async def _expand_bili_parts(video: dict, sel: list[int]) -> list[dict]:
    """分P选择展开：请求条目 + P 号数组 → per-P 条目（长度恒等于去重后的
    P 号数）。重复 P 号先去重保序（评审：[2,2] 曾展开双任务并发写同一
    文件、前端同键双卡；与前端 new Set 同规则）。

    前端按同规则从 args 合成卡片（P 号序、BVxxx-pN 键），results 与卡片
    一一对应（索引对齐契约）。可执行条目带 ?p= URL（下方检测自然跳过）；
    无法获取清单 / 无效 P 号 → 对应位置 error 占位（诚实失败，不静默降级
    到 P1——用户已显式选择）；同一请求内其余分P独立执行不受影响。
    """
    from vidagent.tools.platforms import bilibili

    sel = list(dict.fromkeys(sel))

    bvid = bilibili.extract_bvid(video.get("video_url", "") or "")
    base_title = video.get("title") or bvid or "未知视频"
    if not bvid:
        return [
            {**video, "_error": "parts 仅适用于 B站视频（URL 需含 BV 号）"}
            for _ in sel
        ]
    try:
        # 整体超时兜底（评审：_get 3 次重试最坏 ~48s，会推迟占位注册致
        # 卡片 404 闪烁；超时走既有 error 占位）
        parts = await asyncio.wait_for(bilibili.parts_for_bvid(bvid), timeout=20.0)
    except TimeoutError:
        parts = None
    if parts is None:
        return [
            {
                **video,
                "video_url": bilibili.part_url(bvid, n),
                "video_id": f"{bvid}-p{n}",
                "_error": "分P清单获取失败，请稍后重试",
            }
            for n in sel
        ]
    by_page = {p["page"]: p for p in parts}
    entries = []
    for n in sel:
        p = by_page.get(n)
        entry = {
            **video,
            "video_url": bilibili.part_url(bvid, n),
            "video_id": f"{bvid}-p{n}",
            "platform": "bilibili",
        }
        if p is None:
            entry["_error"] = f"无效分P号 P{n}（有效范围 1-{len(parts)}）"
        else:
            entry["title"] = bilibili.part_title(base_title, p)
            entry["duration_text"] = p["duration_text"]
            entry["duration"] = p["duration"]
        entries.append(entry)
    return entries


@app.post("/api/tools/batch-summarize")
async def tool_batch_summarize(req: BatchSummarizeRequest):
    """批量并行总结：接受视频列表，后端并行处理下载+总结。

    每个视频独立：下载 → 抽取音频+帧 → Omni 多模态总结。
    各视频完全独立，一视频失败不影响其他。下载/总结各重试最多 5 次（指数退避）。

    B站分P视频（裸 BV 未指定 ?p=）不下载：该条目返回 status="multi_part"
    （含 parts 清单与可照抄的 entries 条目），由模型询问用户后重调。
    分P选择展开：条目带 parts（P 号数组）时服务端按 pagelist 展开为 per-P
    条目执行（URL/标题/键全服务端生成）；无效 P 号/清单获取失败 → 该分P
    error 占位（诚实失败），其余分P独立执行。

    返回：
        { batch_id, results: [{ video_id, status, ... }] }
    前端可立即按 video_id 连接 SSE 获取各视频流式进度。
    """
    from vidagent.tools.downloader import MAX_RETRIES, download_video_with_retry
    from vidagent.tools.summarize import (
        ProgressStage,
        cleanup_progress,
        create_progress,
        extract_and_summarize,
    )

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    tasks: list[dict] = []

    def _run_one(video: dict, task_entry: dict) -> None:
        """单个视频的批处理 worker：下载（重试）→ 总结 → 簿记。

        总结管线本体（分流/抽取/降级）在 summarize.extract_and_summarize
        （#4 Q3 深模块）；此处只剩 HTTP 层簿记与下载编排。
        """
        task_id = video["_task_id"]
        video_id = video["_video_id"]

        try:
            # ── 提前创建 progress（下载阶段也需要推送进度）──
            pg = create_progress(task_id)
            pg.stage = ProgressStage.DOWNLOADING

            video_url = video.get("video_url", "")

            # 下载进度回调（pg 已在 try 开头创建）
            # yt-dlp 分多流下载（video+audio），每条流独立报 0→100%
            # 因此进度只升不降，避免看到"满→空→满"的视觉回退
            def _dl_progress(pct: int) -> None:
                if pct > pg.download_pct:
                    pg.download_pct = pct

            local_path, last_err = download_video_with_retry(
                video_url, video_id, progress_callback=_dl_progress,
            )

            if not local_path:
                _summarize_tasks[task_id].fail(f"下载失败(已重试{MAX_RETRIES}次): {last_err}")
                cleanup_progress(task_id)
                _video_task_map.pop(video_id, None)
                task_entry["status"] = "error"
                return

            _summarize_tasks[task_id].local_path = local_path

            metadata = {
                "title": video.get("title", ""),
                "desc": video.get("desc", ""),
                "video_id": video_id,
                "author": video.get("author"),
                "duration_text": video.get("duration_text"),
            }
            summary = extract_and_summarize(local_path, metadata, task_id=task_id)

            _summarize_tasks[task_id].status = TaskStatus.DONE
            _summarize_tasks[task_id].result = summary
            task_entry["status"] = "done"
            logger.info(
                "批量总结完成: %s (%s)",
                video_id, video.get("title", ""),
            )

        except Exception as e:
            logger.exception("批量总结任务 %s 异常", video_id)
            _summarize_tasks[task_id].fail(str(e))
            task_entry["status"] = "error"
        finally:
            cleanup_progress(task_id)

    # ── 分P选择展开（parts 参数）+ B站分P检测（裸 BV 兜底引导）──
    # 展开条目与前端按同规则合成（P 号序、BVxxx-pN 键），results 与卡片索引对齐
    video_list: list[dict] = []
    for v in req.videos:
        video = v.model_dump()
        sel = video.pop("parts", None)
        if sel:
            video_list.extend(await _expand_bili_parts(video, sel))
        else:
            video_list.append(video)
    detects = await asyncio.gather(
        *(_detect_bili_multi_part(v.get("video_url", "")) for v in video_list)
    )

    def _register_pending(video_id: str) -> str:
        """待定条目（multi 引导/展开错误占位）注册 PROCESSING 任务：call 态
        前端会对每条 args 开 by-video SSE，不注册则 5s 轮询 404 → 卡片被
        错误态覆盖（混批时持续到整批完成）。注册后不产事件、流静默保持，
        终态由 POST 返回的 results 驱动。"""
        task_id = uuid.uuid4().hex[:12]
        _video_task_map[video_id] = task_id
        _summarize_tasks[task_id] = TaskRecord(status=TaskStatus.PROCESSING)
        return task_id

    def _derive_video_id(video: dict, task_id: str) -> str:
        return (
            video.get("video_id")
            or _extract_video_id(video.get("video_url", ""))
            or f"vid_{abs(hash(video.get('video_url') or video.get('title', task_id))) & 0xFFFFFFFF:08x}"
        )

    prebaked: dict[int, dict] = {}  # 请求序号 → 预定制结果（展开错误占位）
    multi_by_index: dict[int, dict] = {}
    runnable: list[tuple[int, dict]] = []  # (请求序号, video)
    for i, video in enumerate(video_list):
        err = video.pop("_error", None)
        if err:
            # 展开错误占位：该分P诚实失败（不执行），其余分P独立执行
            video_id = _derive_video_id(video, "pending")
            _register_pending(video_id)
            video["_video_id"] = video_id
            prebaked[i] = {
                "video_id": video_id,
                "video_url": video.get("video_url", ""),
                "title": video.get("title") or video_id,
                "status": "error",
                "error": err,
            }
        elif detects[i]:
            multi_by_index[i] = detects[i]
            video["_video_id"] = _derive_video_id(video, "multi")
            _register_pending(video["_video_id"])
        else:
            runnable.append((i, video))

    # 预注册 runnable 的 task（SSE 连接在 POST 请求期间就会到达，需提前建立映射）
    tasks: list[tuple[int, dict]] = []  # (请求序号, task_entry)
    for i, video in runnable:
        task_id = uuid.uuid4().hex[:12]
        video_id = _derive_video_id(video, task_id)
        video["_task_id"] = task_id
        video["_video_id"] = video_id
        _video_task_map[video_id] = task_id
        _summarize_tasks[task_id] = TaskRecord(status=TaskStatus.PROCESSING)
        tasks.append((i, {"task_id": task_id, "video_id": video_id, "status": "processing"}))

    # runnable 并行提交到线程池（task_entry 按序对应：多线程各自写自己的状态，
    # 避免旧实现 tasks[-1] 共享最后一个条目导致的跨任务状态覆盖）
    futures = []
    for (_, video), (_, task_entry) in zip(runnable, tasks, strict=True):
        futures.append(_executor.submit(_run_one, video, task_entry))

    logger.info("批量总结启动: batch=%s, %d 个请求条目, 展开 %d 个分P, 分P引导 %d 个, 等待完成...",
                batch_id, len(req.videos), len(video_list) - len(req.videos),
                len(multi_by_index))
    logger.info("批量总结注册键: %s", [v["_video_id"] for _, v in runnable])

    # ★ 关键：等待移到线程池，释放 asyncio 事件循环给 SSE 请求
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: [f.result() for f in futures])

    # 汇总结果（按展开后顺序：multi_part 引导/展开错误占位与正常结果交错；
    # 与前端 expandBatchVideos 合成的卡片索引一致）
    # 构建 video_id → 展开后条目 的映射（用于 title + video_url 回填）
    req_by_id: dict[str, dict] = {}
    for v in video_list:
        rid = v.get("video_id") or _extract_video_id(v.get("video_url") or "")
        if rid:
            req_by_id[rid] = v
    results: list[dict | None] = [None] * len(video_list)
    for i, t in tasks:
        task_data = _summarize_tasks.get(t["task_id"])
        video_id = t["video_id"]
        req_v = req_by_id.get(video_id, {})
        if task_data is not None and task_data.status is TaskStatus.DONE:
            results[i] = {
                "video_id": video_id,
                "video_url": req_v.get("video_url", ""),
                "title": req_v.get("title") or video_id,
                # 分P展开条目带单P时长（评审修复：卡片原先显示合集总时长）
                "duration_text": req_v.get("duration_text"),
                "status": "done",
                "summary": task_data.result or "",
                "local_path": task_data.local_path or "",
            }
        else:
            results[i] = {
                "video_id": video_id,
                "status": "error",
                # 旧 .get("result", 默认) 只在键缺失时兜底；此处等价改为仅 None 兜底，
                # 空串错误信息（str(e) == ""）必须原样透传而非被默认值遮蔽
                "error": task_data.result if task_data is not None and task_data.result is not None else "未知错误",
            }
    for i, multi in multi_by_index.items():
        video = video_list[i]
        from vidagent.tools.platforms.bilibili import (
            PARTS_LIST_CAP,
            part_url,
        )
        from vidagent.tools.platforms.bilibili import (
            part_title as bilibili_part_title,
        )

        video_id = video.get("_video_id") or multi["bvid"]
        base_title = video.get("title") or multi["bvid"]
        # 清单/条目上送截断（防千P视频撑爆上下文；total_parts 仍是真实总数，
        # parts 少于 total_parts 即截断——用户仍可按 P 号指定任意分P）
        shown = multi["parts"][:PARTS_LIST_CAP]
        entries = []
        for p in shown:
            entries.append({
                "video_url": part_url(multi["bvid"], p["page"]),
                "video_id": f"{multi['bvid']}-p{p['page']}",
                "title": bilibili_part_title(base_title, p),
                "desc": video.get("desc") or "",
                "author": video.get("author"),
                "duration": p.get("duration"),
                "duration_text": p.get("duration_text"),
                "platform": "bilibili",
            })
        results[i] = {
            "video_id": video_id,
            # canonical 地址（评审修复：入参为 b23.tv 短链时，模型按引导以
            # parts 参数重调需要含 BV 号的地址——原样回传短链会让展开
            # 提取不到 BV 而误报「parts 仅适用于 B站视频」）
            "video_url": f"https://www.bilibili.com/video/{multi['bvid']}",
            "title": base_title,
            "status": "multi_part",
            "total_parts": multi["total_parts"],
            "parts": shown,
            "entries": entries,
            "message": (
                f"该视频是分P视频（共 {multi['total_parts']} 个分P），未指定分P，未总结。"
                "请告知用户这是分P视频并询问要总结哪一P或全部；"
                "用户答复后以 parts 参数传所选 P 号数组重调本条目"
                f"（如 parts:[2]，全部为 parts:[1..{multi['total_parts']}]）；"
                "下载场景用 entries 条目的 URL。"
            ),
        }

    for i, res in prebaked.items():
        results[i] = res

    # 清理 video_id → task_id 映射（runnable + multi 待选卡 + 展开错误占位）
    for _, video in runnable:
        _video_task_map.pop(video["_video_id"], None)
    for i in multi_by_index:
        _video_task_map.pop(video_list[i]["_video_id"], None)
    for i in prebaked:
        _video_task_map.pop(video_list[i]["_video_id"], None)

    logger.info("批量总结完成: batch=%s, done=%d/%d", batch_id,
                sum(1 for r in results if r and r["status"] == "done"), len(results))
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


@app.get("/api/meta")
async def api_meta():
    """Provider 元数据（最小形状，调优专项批次⑤）。

    前端 route.ts 据 relay_mode 条件化 SYSTEM_PROMPT：XML 工具调用协议段
    仅 xml 模式拼入（transparent 模式原生 function calling，该段是噪声且
    有诱导模型把 XML 写进 content 的风险）。加字段属非破坏性演进。
    """
    return {"provider": settings.llm_provider, "relay_mode": llm_provider.relay_mode()}


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
