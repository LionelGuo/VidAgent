"""Gradio 对话 UI：自然语言驱动 + Markdown + 最新视频内嵌播放 + 多轮记忆（文档 §2.1）。

启动：uv run python -m vidagent.ui
"""

from __future__ import annotations

import logging
import uuid

import gradio as gr

from vidagent.agent import build_agent
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


async def _bot_step(history, session_id: str):
    """运行 Agent（arun + session_id 实现多轮记忆），追加回复并更新视频窗口。"""
    user_msg = history[-1]["content"]
    try:
        resp = await get_agent().arun(user_msg, session_id=session_id)
        text = getattr(resp, "content", None) or str(resp)
        dur = getattr(getattr(resp, "metrics", None), "duration", None)
        if dur:
            logger.info("⏱ 本轮 Agent 总耗时 %.2fs", dur)
    except Exception as e:  # 不让 UI 崩溃
        text = f"⚠️ 运行出错：{e}"
    history = history + [{"role": "assistant", "content": text}]
    return history, _latest_mp4()


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

        session_state = gr.State(uuid.uuid4().hex)  # 会话 id：多轮记忆的载体

        # 提交链：先把用户消息上屏 → 再跑 Agent（带上 session_id）
        sub = msg.submit(_user_step, [msg, chatbot], [msg, chatbot])
        clk = btn.click(_user_step, [msg, chatbot], [msg, chatbot])
        for ev in (sub, clk):
            ev.then(_bot_step, [chatbot, session_state], [chatbot, video])

        clear_btn.click(_clear_chat, outputs=[chatbot, video, session_state])

    return demo


if __name__ == "__main__":
    setup_logging()
    build_ui().launch()
