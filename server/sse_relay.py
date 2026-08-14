"""SSE 流式转发器：vLLM bare mode → 实时 tool_call 检测与转换。

核心状态机：
  PASSTHROUGH ──检测到 <tool_call──→ BUFFERING
       ↑                                    │
       └────── 提取完成，发送 tool_calls ────┘

vLLM 以 bare mode 运行（无 --enable-auto-tool-choice，无 --tool-call-parser），
模型自由输出文本（可能包含 <think> 推理 + <tool_call> XML）。本模块逐 SSE chunk 流式处理：
- 纯文本（含 <think>…</think>）→ 直接透传（AI SDK 的 extractReasoningMiddleware 负责解析推理）
- <tool_call> → 缓冲完整 XML → 提取 JSON → 构造 OpenAI tool_calls delta SSE
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

# 前缀扫描：缓冲首段内容以检测 tool_call / think
_SCAN_BUFFER_SIZE = 20

# <tool_call> XML 标签模式
_TOOL_CALL_START = "<tool_call"
_TOOL_CALL_PATTERN = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)



def _extract_tool_call(text: str) -> dict | None:
    """从文本中提取单个 tool_call，多层容错解析。

    Returns:
        {"name": str, "arguments": dict} | None
    """
    match = _TOOL_CALL_PATTERN.search(text)
    if not match:
        return None

    json_str = match.group(1).strip()
    if not json_str:
        return None

    # ── 方法 1：直接 json.loads ──
    try:
        data = json.loads(json_str)
        return {
            "name": data.get("name", ""),
            "arguments": data.get("arguments", {}),
        }
    except json.JSONDecodeError:
        pass

    # ── 方法 2：大括号计数 ──
    brace_start = json_str.find("{")
    if brace_start != -1:
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
                        break  # 方法 2 失败，继续方法 3

    # ── 方法 3：正则宽松提取（最后兜底）──
    import re as _re
    name_m = _re.search(r'"name"\s*:\s*"([^"]*)"', json_str)
    if not name_m:
        return None

    name = name_m.group(1)
    # 尝试提取 arguments（用大括号计数，从 "arguments" 后的 { 开始）
    args_keyword = _re.search(r'"arguments"\s*:\s*', json_str)
    if args_keyword:
        args_start = args_keyword.end()
        if args_start < len(json_str) and json_str[args_start] == "{":
            depth = 0
            for i, ch in enumerate(json_str[args_start:], args_start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            arguments = json.loads(json_str[args_start : i + 1])
                            return {"name": name, "arguments": arguments}
                        except json.JSONDecodeError:
                            break

    # 至少返回 name
    return {"name": name, "arguments": {}}


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
    model: str | None = None,
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
    # 注入真实模型名（前端发占位符，由 provider 预设解析）
    if model:
        body["model"] = model

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    state = "PASSTHROUGH"
    buffer = ""
    yielded_text = ""
    sent_tool_calls = False
    last_finish_reason = "stop"

    logger.info("SSE Relay 开始 -> vLLM: %s", vllm_url)

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
                                logger.warning("流结束时 tool_call buffer 不完整,回退为文本")
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
                                    "检测到 <tool_call,切换 BUFFERING | buffer=%d chars",
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
                                # 可能含多个 <tool_call>：循环提取，保留剩余
                                while "</tool_call>" in buffer:
                                    end_tag_pos = buffer.find("</tool_call>")
                                    block_end = end_tag_pos + len("</tool_call>")
                                    block = buffer[:block_end]

                                    tool_call = _extract_tool_call(block)
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
                                            "tool_call 提取失败,回退为纯文本 | len=%d head=%r tail=%r",
                                            len(block), block[:100], block[-100:],
                                        )
                                        yield _format_sse_content(block)

                                    # 保留 </tool_call> 之后的文本，继续检测
                                    buffer = buffer[block_end:]

                                # 所有 tool_call 处理完 → 检查剩余 buffer
                                if _TOOL_CALL_START in buffer:
                                    # 还有未闭合的 <tool_call，继续缓冲
                                    state = "BUFFERING"
                                else:
                                    # 剩余是纯文本，透传
                                    if buffer:
                                        yield _format_sse_content(buffer)
                                        yielded_text += buffer
                                    buffer = ""
                                    state = "PASSTHROUGH"

            # 正常结束（没有 data: [DONE] 行）
            if buffer:
                if state == "BUFFERING":
                    logger.warning("流结束时 tool_call buffer 不完整,回退为文本")
                yield _format_sse_content(buffer)
            yield _yield_finish_reason_chunk(sent_tool_calls, last_finish_reason)
            yield "data: [DONE]\n\n"
            break  # 成功 → 跳出重试循环

        except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "vLLM 连接超时 (attempt %d/%d),%ds 后重试...",
                    attempt, max_retries, wait,
                )
                await _asyncio.sleep(wait)
            else:
                logger.error("vLLM 连接失败,已达最大重试次数: %s", e)
                yield _format_sse_content(
                    "⚠️ vLLM 服务连接超时，请稍后重试。如持续出现请联系管理员检查服务器状态。"
                )
                yield "data: [DONE]\n\n"

    logger.info("SSE Relay 完成 | yielded_text=%d chars", len(yielded_text))


async def relay_stream_transparent(
    request_body: dict,
    upstream_url: str,
    model: str,
    api_key: str = "not-needed",
    timeout: int = 300,
) -> AsyncIterator[str]:
    """原生透传 relay（用于支持原生 function calling 的标准 OpenAI 兼容端点，如 SiliconFlow）。

    与 relay_stream（XML 模式，面向 vLLM bare mode）的区别：
    - 保留 tools（让模型原生 function calling，AI SDK 原生解析 tool_calls）
    - tool_choice 规范化为 auto（SiliconFlow 等仅支持 auto，避免 400）
    - 注入 model（前端发占位符，服务端按 provider 预设注入）
    - reasoning_content → <think> 文本流转换：@ai-sdk/openai 的 chat 流解析不读
      delta.reasoning_content（会直接丢弃），转换成 <think> 标签文本后由前端
      extractReasoningMiddleware 提取——与 vLLM 路径同一条 reasoning 通道，前端零感知
    - tool_calls / finish_reason 透传
    """
    body = dict(request_body)
    body["model"] = model
    body["stream"] = True
    # SiliconFlow 等仅支持 tool_choice=auto；forced/required 会 400，强制规范化
    if body.get("tool_choice") not in (None, "auto"):
        body["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.info("SSE Relay(transparent) -> %s | model=%s", upstream_url, model)

    in_reasoning = False

    def _flush_reasoning() -> list[str]:
        """闭合未闭合的 <think> 块。"""
        nonlocal in_reasoning
        if in_reasoning:
            in_reasoning = False
            return [_format_sse_content("</think>")]
        return []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            async with client.stream(
                "POST",
                f"{upstream_url}/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    error_bytes = await resp.aread()
                    error_text = error_bytes.decode(errors="ignore")[:400]
                    logger.error(
                        "上游返回错误 HTTP %s: %s", resp.status_code, error_text
                    )
                    yield _format_sse_content(
                        f"⚠️ 模型服务错误 (HTTP {resp.status_code})：{error_text}"
                    )
                    yield "data: [DONE]\n\n"
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_part = line[5:].strip()
                    if data_part == "[DONE]":
                        # 流结束：先闭合未闭合的 <think>（推理后直接结束的极端情况）
                        for sse in _flush_reasoning():
                            yield sse
                        yield "data: [DONE]\n\n"
                        continue
                    try:
                        chunk = json.loads(data_part)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""
                    tool_calls = delta.get("tool_calls")
                    finish = choice.get("finish_reason")

                    # 1) 推理流 → <think> 包裹的 content 流（前端 middleware 提取）
                    if rc:
                        if not in_reasoning:
                            yield _format_sse_content("<think>")
                            in_reasoning = True
                        yield _format_sse_content(rc)

                    # 2) 正文 / tool_calls 到达 → 先闭合 </think> 再透传
                    if content or tool_calls is not None:
                        for sse in _flush_reasoning():
                            yield sse
                        if content:
                            yield _format_sse_content(content)
                        if tool_calls is not None:
                            tc_payload = json.dumps(
                                {
                                    "choices": [
                                        {
                                            "delta": {"tool_calls": tool_calls},
                                            "index": choice.get("index", 0),
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                            yield f"data: {tc_payload}\n\n"

                    # 3) finish_reason 透传（触发 AI SDK 续跑 tool_calls）
                    if finish:
                        for sse in _flush_reasoning():
                            yield sse
                        fin_payload = json.dumps(
                            {
                                "choices": [
                                    {
                                        "delta": {},
                                        "finish_reason": finish,
                                        "index": choice.get("index", 0),
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                        yield f"data: {fin_payload}\n\n"
    except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        logger.error("上游连接超时: %s", e)
        yield _format_sse_content("⚠️ 模型服务连接超时，请稍后重试。")
        yield "data: [DONE]\n\n"

    logger.info("SSE Relay(transparent) 完成")
