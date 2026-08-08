"""SSE 流式转发器：vLLM bare mode → 实时 tool_call 检测与转换。

核心状态机：
  PASSTHROUGH ──检测到 <tool_call──→ BUFFERING
       ↑                                    │
       └────── 提取完成，发送 tool_calls ────┘

vLLM 以 bare mode 运行（无 --enable-auto-tool-choice，无 --tool-call-parser），
模型自由输出文本（可能包含 <tool_call> XML）。本模块逐 SSE chunk 流式处理：
- 纯文本 → 直接透传
- <tool_call> → 缓冲完整 XML → 提取 JSON → 构造 OpenAI tool_calls delta SSE
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# 前缀扫描：缓冲首段内容以检测 tool_call
_SCAN_BUFFER_SIZE = 20

# <tool_call> XML 标签模式
_TOOL_CALL_START = "<tool_call"
_TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _extract_tool_call(text: str) -> dict | None:
    """从文本中提取单个 tool_call，使用大括号计数处理嵌套 JSON。

    Returns:
        {"name": str, "arguments": dict} | None
    """
    match = _TOOL_CALL_PATTERN.search(text)
    if not match:
        return None

    json_str = match.group(1).strip()

    # 尝试直接解析
    try:
        data = json.loads(json_str)
        return {
            "name": data.get("name", ""),
            "arguments": data.get("arguments", {}),
        }
    except json.JSONDecodeError:
        pass

    # 大括号计数：找到第一个完整 JSON 对象
    brace_start = json_str.find("{")
    if brace_start == -1:
        return None

    depth = 0
    for i, ch in enumerate(json_str[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(json_str[brace_start : i + 1])
                    return {
                        "name": data.get("name", ""),
                        "arguments": data.get("arguments", {}),
                    }
                except json.JSONDecodeError:
                    return None

    return None  # 括号不闭合


def _format_sse_content(text: str) -> str:
    """格式化纯文本内容为 SSE data 行。"""
    payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _format_sse_tool_call(tool_name: str, arguments: dict, tool_id: str) -> list[str]:
    """格式化 tool_call 为两个 SSE data 行（id+name，然后 arguments）。

    与 OpenAI 流式 tool_calls 格式兼容：第一个 delta 声明 id/type/name，
    第二个 delta 携带完整 arguments JSON。
    """
    args_str = json.dumps(arguments, ensure_ascii=False)

    # Delta 1: id + type + function name
    delta1 = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ]
                },
                "index": 0,
            }
        ]
    }

    # Delta 2: arguments
    delta2 = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": args_str}}
                    ]
                },
                "index": 0,
            }
        ]
    }

    return [
        f"data: {json.dumps(delta1, ensure_ascii=False)}\n\n",
        f"data: {json.dumps(delta2, ensure_ascii=False)}\n\n",
    ]


def _yield_finish_reason_chunk(sent_tool_calls: bool, vllm_finish: str) -> str:
    """生成 finish_reason 的 SSE chunk。

    AI SDK 需要 finish_reason: "tool_calls" 来触发 auto-continuation。
    vLLM bare mode 总是返回 "stop"，当 relay 已发送 tool_calls 时需覆写。
    """
    reason = "tool_calls" if sent_tool_calls else vllm_finish or "stop"
    payload = {
        "choices": [
            {
                "delta": {},
                "finish_reason": reason,
                "index": 0,
            }
        ]
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def relay_stream(
    request_body: dict,
    vllm_url: str,
    api_key: str = "not-needed",
    timeout: int = 300,
) -> AsyncIterator[str]:
    """SSE 流式转发：请求 vLLM → 逐行处理 → 实时 SSE 输出。

    状态机：
    - PASSTHROUGH: 逐 chunk 检查 <tool_call，纯文本直接透传
    - BUFFERING: 缓冲直到 </tool_call> 闭合，提取 JSON，发送 tool_calls delta
    """
    import asyncio as _asyncio

    # 剥离 tool 相关字段（vLLM bare mode 不需要）
    body = {
        k: v
        for k, v in request_body.items()
        if k not in ("tools", "tool_choice", "stream_options")
    }
    body["stream"] = True

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    state = "PASSTHROUGH"
    buffer = ""
    yielded_text = ""
    sent_tool_calls = False
    last_finish_reason = "stop"

    logger.info("SSE Relay 开始 → vLLM: %s", vllm_url)

    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                async with client.stream(
                    "POST",
                    f"{vllm_url}/chat/completions",
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status_code != 200:
                        error_text = await resp.aread()
                        logger.error(
                            "vLLM 返回错误 HTTP %s: %s",
                            resp.status_code,
                            error_text[:500],
                        )
                        yield _format_sse_content(
                            f"⚠️ vLLM 服务错误 (HTTP {resp.status_code})，请检查后端服务状态。"
                        )
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        if line == "data: [DONE]":
                            if buffer and state == "BUFFERING":
                                logger.warning(
                                    "流结束时 buffer 残留（tool_call 可能不完整），回退为文本"
                                )
                                yield _format_sse_content(buffer)
                            yield _yield_finish_reason_chunk(sent_tool_calls, last_finish_reason)
                            yield "data: [DONE]\n\n"
                            return

                        try:
                            chunk_data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        choices = chunk_data.get("choices") or []
                        if not choices:
                            continue

                        delta = choices[0].get("delta") or {}
                        content = delta.get("content", "")

                        # 捕获 finish_reason（vLLM 在最后一个有意义的 chunk 中发送）
                        fr = choices[0].get("finish_reason")
                        if fr:
                            last_finish_reason = fr

                        if not content:
                            continue

                        if state == "PASSTHROUGH":
                            buffer += content
                            tc_pos = buffer.find(_TOOL_CALL_START)
                            if tc_pos != -1:
                                prefix = buffer[:tc_pos]
                                if prefix:
                                    yield _format_sse_content(prefix)
                                    yielded_text += prefix
                                buffer = buffer[tc_pos:]
                                state = "BUFFERING"
                                logger.debug(
                                    "检测到 <tool_call，切换 BUFFERING | buffer=%d chars",
                                    len(buffer),
                                )
                            else:
                                safe_end = len(buffer)
                                for partial in (
                                    "<tool_call", "<tool_cal", "<tool_ca",
                                    "<tool_c", "<tool_", "<tool", "<too",
                                    "<to", "<t",
                                ):
                                    if buffer.endswith(partial):
                                        safe_end = len(buffer) - len(partial)
                                        break
                                if safe_end > 0:
                                    safe_text = buffer[:safe_end]
                                    yield _format_sse_content(safe_text)
                                    yielded_text += safe_text
                                    buffer = buffer[safe_end:]

                        elif state == "BUFFERING":
                            buffer += content
                            if "</tool_call>" in buffer:
                                logger.debug(
                                    "收到完整 </tool_call> | buffer=%d chars",
                                    len(buffer),
                                )
                                tool_call = _extract_tool_call(buffer)
                                if tool_call and tool_call["name"]:
                                    tool_id = f"call_{uuid.uuid4().hex[:24]}"
                                    logger.info(
                                        "提取 tool_call: %s(%s)",
                                        tool_call["name"],
                                        json.dumps(
                                            tool_call["arguments"],
                                            ensure_ascii=False,
                                        )[:120],
                                    )
                                    for sse_line in _format_sse_tool_call(
                                        tool_call["name"],
                                        tool_call["arguments"],
                                        tool_id,
                                    ):
                                        yield sse_line
                                    sent_tool_calls = True
                                else:
                                    logger.warning(
                                        "tool_call 提取失败，回退为纯文本 | buffer=%r",
                                        buffer[:200],
                                    )
                                    yield _format_sse_content(buffer)
                                buffer = ""
                                state = "PASSTHROUGH"

            # 正常结束（没有 data: [DONE] 行）
            if buffer:
                if state == "BUFFERING":
                    logger.warning("流结束时有未完成的 tool_call buffer，回退为文本")
                yield _format_sse_content(buffer)
            yield _yield_finish_reason_chunk(sent_tool_calls, last_finish_reason)
            yield "data: [DONE]\n\n"
            break  # 成功 → 跳出重试循环

        except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "vLLM 连接超时 (attempt %d/%d)，%ds 后重试…",
                    attempt, max_retries, wait,
                )
                await _asyncio.sleep(wait)
            else:
                logger.error("vLLM 连接失败，已达最大重试次数: %s", e)
                yield _format_sse_content(
                    "⚠️ vLLM 服务连接超时，请稍后重试。如持续出现请联系管理员检查服务器状态。"
                )
                yield "data: [DONE]\n\n"

    logger.info("SSE Relay 完成 | yielded_text=%d chars", len(yielded_text))
