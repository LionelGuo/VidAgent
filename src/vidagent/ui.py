"""Gradio 对话 UI：自然语言驱动 + Markdown + 最新视频内嵌播放 + 多轮记忆（文档 §2.1）。

启动：uv run python -m vidagent.ui
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import gradio as gr

from vidagent.agent import build_agent
from vidagent.tools import summarizer
from vidagent.utils import storage
from vidagent.utils.logging import setup_logging

logger = logging.getLogger(__name__)

_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


def _latest_mp4() -> str | None:
    mp4s = sorted(
        storage.workspace().glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return str(mp4s[0]) if mp4s else None


def _user_step(user_msg: str, history):
    """把用户消息加入历史并清空输入框。"""
    return "", history + [{"role": "user", "content": user_msg}]


_TOOL_LABELS = {
    "get_hot_videos": "热门",
    "search_videos": "搜索",
    "get_creator_videos": "创作者",
    "download_video": "下载视频",
    "extract_and_summarize": "转写+总结",
    "search_and_fetch_videos": "检索视频",  # 旧名，保留无害
}


def _render_status(running: list, answer: str) -> str:
    """组装 assistant 消息：顶部阶段进度行 + 下方流式回答。"""
    parts = []
    if running:
        badges = " · ".join(("✅" if done else "⏳") + lbl for done, lbl in running)
        parts.append(f"<sub>{badges}</sub>")
    if answer:
        parts.append(answer)
    return "\n\n".join(parts) if parts else "⏳ 思考中…"


_RENDER_INTERVAL = 0.1  # 100ms 刷新一次 UI
_RENDER_SENTINEL = object()


async def _bot_step(history, session_id: str):
    """流式运行 Agent，定时渲染（100ms）而非逐事件刷新。

    Agno 产生 ~700 碎片事件（每 1-2 字符），逐个 yield 给 Gradio 会导致
    UI 线程忙于序列化+DOM 更新。改为短超时轮询：事件无阻塞累积，
    每 100ms 超时触发一次 snapshot yield。
    """
    user_msg = history[-1]["content"]
    history = history + [{"role": "assistant", "content": ""}]
    idx = len(history) - 1
    running: list = []  # [(done?, 标签), ...]
    answer_parts: list[str] = []
    t0 = time.perf_counter()

    def snapshot():
        body = _render_status(running, "".join(answer_parts))
        history[idx]["content"] = body
        return history, _latest_mp4()

    queue: asyncio.Queue = asyncio.Queue()

    async def consume():
        try:
            async for ev in get_agent().arun(
                user_msg, session_id=session_id, stream=True, stream_events=True
            ):
                await queue.put(ev)
        except Exception as e:
            await queue.put(e)
        finally:
            await queue.put(None)

    task = asyncio.create_task(consume())
    last_yield = t0

    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=_RENDER_INTERVAL)
            except TimeoutError:
                # 100ms 无事件 → 检查直播状态 + 按时刷新 UI
                partial = summarizer.live_partial()
                summary_live = summarizer.live_summary()
                now = time.perf_counter()
                if partial or summary_live or answer_parts or running:
                    if now - last_yield >= _RENDER_INTERVAL:
                        yield snapshot()
                        last_yield = now
                continue
            if ev is None:
                break
            if isinstance(ev, Exception):
                raise ev
            etype = type(ev).__name__
            logger.info("Event: %s content=%s", etype,
                        repr(str(getattr(ev, "content", ""))[:60]))
            if etype == "ToolCallStartedEvent":
                name = getattr(getattr(ev, "tool", None), "tool_name", "") or "工具"
                running.append((False, _TOOL_LABELS.get(name, name)))
            elif etype == "ToolCallCompletedEvent":
                if running and not running[-1][0]:
                    running[-1] = (True, running[-1][1])
                name = getattr(getattr(ev, "tool", None), "tool_name", "") or ""
                if name == "extract_and_summarize":
                    result = getattr(ev, "content", None)
                    if isinstance(result, str) and result.strip():
                        answer_parts.append(result)
            elif etype == "RunContentEvent":
                delta = getattr(ev, "content", "") or ""
                if delta:
                    answer_parts.append(delta)
                else:
                    r = getattr(ev, "reasoning_content", "") or ""
                    if r:
                        answer_parts.append(r)
            elif etype == "RunCompletedEvent":
                # 最终事件：可能包含完整回答（某些 Agno 版本仅在此事件中带 content）
                content = getattr(ev, "content", "") or ""
                if content and not answer_parts:
                    answer_parts.append(content)
            elif etype == "RunResponse":
                # 非流式兼容：arun() 直接返回 RunResponse 对象
                content = getattr(ev, "content", "") or ""
                if content:
                    answer_parts.append(content)
            # 事件累积后不立即 yield；等待 timeout 统一刷新
        # 完成
        final = "".join(answer_parts).strip()
        history[idx]["content"] = _render_status(running, final) or final
        yield history, _latest_mp4()
    except Exception as e:
        history[idx]["content"] = f"⚠️ 运行出错：{e}"
        yield history, _latest_mp4()
    finally:
        if not task.done():
            task.cancel()
    logger.info("⏱ 本轮 Agent 总耗时 %.2fs", time.perf_counter() - t0)


def _clear_chat():
    """清空对话：重置界面并开新会话（新 session_id → 无历史记忆）。"""
    return [], None, uuid.uuid4().hex


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="VidAgent") as demo:
        gr.Markdown(
            "# 🎬 VidAgent — 视频采集与多模态总结助手\n"
            "自然语言驱动，支持多轮对话。例如：「抓 B站 今日热榜前 3 并逐个总结」"
        )
        with gr.Row():
            chatbot = gr.Chatbot(height=460, label="对话")
            video = gr.Video(label="最新下载的视频")
        with gr.Row():
            msg = gr.Textbox(
                placeholder="抓 B站 今日热榜前 3 并逐个总结 / 总结老番茄最近的视频",
                scale=4,
                label="指令",
            )
            btn = gr.Button("发送", variant="primary", scale=1)
            clear_btn = gr.Button("清空对话", scale=1)

        gr.Examples(
            examples=[
                "B站今日热榜前2名是什么？",
                "抓B站今日热榜前1名并总结",
                "总结老番茄最近2个视频",
            ],
            inputs=msg,
        )

        session_state = gr.State(uuid.uuid4().hex)

        sub = msg.submit(_user_step, [msg, chatbot], [msg, chatbot])
        clk = btn.click(_user_step, [msg, chatbot], [msg, chatbot])
        for ev in (sub, clk):
            ev.then(_bot_step, [chatbot, session_state], [chatbot, video])

        clear_btn.click(_clear_chat, outputs=[chatbot, video, session_state])

    return demo


if __name__ == "__main__":
    setup_logging()
    build_ui().launch()
