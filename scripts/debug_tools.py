#!/usr/bin/env python3
"""调试工具：观察模型对给定提示词的工具调用行为。

直接连接 SSE Relay，展示模型输出的原始 tool_call（未经 AI SDK 处理），
用于手动排查模型是否调用了错误的工具或传了错误的参数。

用法：
  uv run python scripts/debug_tools.py "搜索python教程"
  uv run python scripts/debug_tools.py "B站热榜前3名" --no-stream
  uv run python scripts/debug_tools.py "总结老番茄最近的视频" --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urljoin

import httpx

# ── 配置 ──────────────────────────────────────────────────
DEFAULT_API = "http://127.0.0.1:8000"
SYSTEM_PROMPT = """你是 VidAgent，一个自媒体视频采集与总结助手，通过调用工具完成用户的自然语言指令。

【可用工具】
- get_hot_videos(platform, limit, date_filter)：获取平台综合热门/榜单视频。
- search_videos(platform, keyword, limit, date_filter)：按关键词搜索视频。
- get_creator_videos(platform, creator, limit, date_filter)：获取指定创作者(UP主)的视频。
- download_video(video_url, file_name)：下载视频到本地，返回 local_path。
- extract_and_summarize(local_path, metadata)：对本地视频生成结构化总结。

【工具调用格式】当需要使用工具时，用以下格式（不要用 markdown 代码块包裹）：
<tool_call>
{"name": "工具名", "arguments": {...}}
</tool_call>

【检索工具选择】
- 用户提到具体 UP 主/创作者人名 → 用 get_creator_videos。
- 用户用关键词描述内容（如"Python教程""搞笑视频"）→ 用 search_videos。
- 用户想看热门/榜单 → 用 get_hot_videos。
- date_filter 默认不传。只在用户明确说「只看今天发布的」时才传 "today"。

【其它】
- 平台默认 "bilibili"。
- 全程中文。
"""

# ── 颜色 ──────────────────────────────────────────────────
C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}
def _c(code: str, text: str) -> str:
    return f"{C.get(code, '')}{text}{C['reset']}"


def extract_tool_calls(text: str) -> list[dict]:
    """从文本中提取所有 <tool_call> XML 块"""
    pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    results = []
    for match in pattern.finditer(text):
        try:
            data = json.loads(match.group(1))
            results.append({"name": data.get("name", "?"), "arguments": data.get("arguments", {})})
        except json.JSONDecodeError:
            results.append({"name": "PARSE_ERROR", "arguments": {}, "raw": match.group(1)[:200]})
    return results


async def main():
    parser = argparse.ArgumentParser(description="调试模型工具调用行为")
    parser.add_argument("prompt", help="提示词")
    parser.add_argument("--api", default=DEFAULT_API, help=f"SSE Relay 地址 (默认 {DEFAULT_API})")
    parser.add_argument("--no-stream", action="store_true", help="非流式模式（等待完整响应）")
    parser.add_argument("--max-tokens", type=int, default=2048, help="最大生成 token 数")
    parser.add_argument("--raw", action="store_true", help="同时输出原始 SSE chunk")
    args = parser.parse_args()

    payload = {
        "model": "/root/autodl-tmp/Qwen3-Omni-30B-AWQ",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": args.prompt},
        ],
        "stream": not args.no_stream,
        "max_tokens": args.max_tokens,
    }

    print(_c("bold", f"\n🔍 提示词: {args.prompt}\n"))
    print(_c("dim", "─" * 60))

    if args.no_stream:
        # 非流式
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                urljoin(args.api, "/v1/chat/completions"),
                json=payload,
            )
            if resp.status_code != 200:
                print(_c("red", f"HTTP {resp.status_code}: {resp.text[:500]}"))
                return
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            print(content)
    else:
        # 流式
        tool_calls = []
        text_parts: list[str] = []
        current_text: list[str] = []

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                urljoin(args.api, "/v1/chat/completions"),
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(_c("red", f"HTTP {resp.status_code}: {body[:500]}"))
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        break

                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue

                    delta = choices[0].get("delta") or {}

                    # 检查 tool_calls
                    tc_list = delta.get("tool_calls") or []
                    for tc in tc_list:
                        fn = tc.get("function") or {}
                        name = fn.get("name")
                        args_str = fn.get("arguments", "")

                        if name:
                            # 新的 tool_call 开始
                            tc_info = {"name": name, "arguments": args_str}
                            tool_calls.append(tc_info)
                            # 刷新之前的文本
                            if current_text:
                                text = "".join(current_text)
                                text_parts.append(text)
                                sys.stdout.write(_c("dim", text))
                                sys.stdout.flush()
                                current_text = []

                            print(_c("yellow", f"\n📞 调用工具: {name}"))
                            print(_c("blue", f"   ID: {tc.get('id', '?')}"))

                        elif args_str and tool_calls:
                            # 追加 arguments
                            tool_calls[-1]["arguments"] += args_str

                    # 检查纯文本内容
                    content = delta.get("content", "")
                    if content:
                        current_text.append(content)
                        sys.stdout.write(content)
                        sys.stdout.flush()

                    fr = choices[0].get("finish_reason")
                    if fr and args.raw:
                        print(_c("dim", f"\n[finish_reason: {fr}]"))

        # 最终输出
        if current_text:
            text_parts.append("".join(current_text))

        # ── 工具调用汇总 ──
        if tool_calls:
            print("\n")
            print(_c("dim", "─" * 60))
            print(_c("bold", "📊 工具调用汇总:"))
            for i, tc in enumerate(tool_calls, 1):
                print(_c("yellow", f"\n  [{i}] {tc['name']}"))
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    for k, v in args.items():
                        val_str = str(v)
                        if len(val_str) > 120:
                            val_str = val_str[:120] + "…"
                        print(f"      {k}: {_c('cyan', val_str)}")
                except json.JSONDecodeError:
                    print(_c("red", f"      ⚠️ 参数解析失败: {tc['arguments'][:200]}"))
            print(_c("dim", f"\n  共 {len(tool_calls)} 次工具调用"))
        else:
            print(_c("dim", "\n\n(未调用工具 — 纯文本回复)"))

    print(_c("dim", "\n" + "─" * 60))
    print(_c("green", "✅ 完成\n"))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
