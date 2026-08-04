"""Gradio 对话 UI：自然语言驱动 + Markdown 渲染 + 最新视频内嵌播放（文档 §2.1）。

启动：uv run python -m vidagent.ui
"""

from __future__ import annotations

import gradio as gr

from vidagent.agent import build_agent
from vidagent.utils import storage

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


async def _bot_step(history):
    """运行 Agent（arun 以支持异步工具），追加回复并更新视频窗口。"""
    user_msg = history[-1]["content"]
    try:
        resp = await get_agent().arun(user_msg)
        text = getattr(resp, "content", None) or str(resp)
    except Exception as e:  # 不让 UI 崩溃
        text = f"⚠️ 运行出错：{e}"
    history = history + [{"role": "assistant", "content": text}]
    return history, _latest_mp4()


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="VidAgent") as demo:
        gr.Markdown(
            "# 🎬 VidAgent — 视频采集与多模态总结助手\n"
            "自然语言驱动。例如：「抓 B站 今日热榜前 3 并逐个总结」"
        )
        with gr.Row():
            chatbot = gr.Chatbot(height=460, label="对话")
            video = gr.Video(label="最新下载的视频")
        with gr.Row():
            msg = gr.Textbox(
                placeholder="抓 B站 今日热榜前 3 并逐个总结 / 搜索「大模型」并总结第一条",
                scale=4,
                label="指令",
            )
            btn = gr.Button("发送", variant="primary", scale=1)

        gr.Examples(
            examples=[
                "B站今日热榜前2名是什么？",
                "抓B站今日热榜前1名并总结",
                "搜索B站「大模型」并总结第一个视频",
            ],
            inputs=msg,
        )

        # 提交链：先把用户消息上屏 → 再跑 Agent
        sub = msg.submit(_user_step, [msg, chatbot], [msg, chatbot])
        clk = btn.click(_user_step, [msg, chatbot], [msg, chatbot])
        for ev in (sub, clk):
            ev.then(_bot_step, [chatbot], [chatbot, video])

    return demo


if __name__ == "__main__":
    build_ui().launch()
