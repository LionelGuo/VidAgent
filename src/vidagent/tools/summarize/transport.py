"""LLM 传输层（#4 深模块：自原 summarizer.py 拆出）。

非流式 / 流式 chat completion + <think> 推理块过滤。所有管线共享的 hub：
_prompt 常量以外的全部 LLM 请求都经 _chat_completion / _chat_completion_stream。

全局 LLM 并发闸（#4 Q4）：原 main.py 的 _llm_semaphore 移入本层，
所有请求统一受控（原仅 batch 短视频路径受控，长路径与单视频端点无闸）。
"""

from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from vidagent import llm_provider
from vidagent.tools.summarize.progress import Progress, ProgressStage

logger = logging.getLogger(__name__)

# vLLM 并发控制：避免多视频同时 Omni 推理导致显存/队列拥塞
# （自 server/main.py 移入；语义收紧为全局 LLM 并发 ≤2，#4 Q4 已批准）
_llm_semaphore = threading.BoundedSemaphore(2)


def _chat_completion(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
) -> str:
    """非流式 chat completion，返回完整响应文本（自动剥离 <think> 块）。"""
    t0 = time.perf_counter()
    with _llm_semaphore:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 调用失败 HTTP {resp.status_code}: {resp.text[:300]}")
        raw = resp.json()["choices"][0]["message"]["content"]
        usage = resp.json().get("usage", {})
    stripped = _strip_think_blocks(raw)
    elapsed = time.perf_counter() - t0
    think_len = len(raw) - len(stripped)
    logger.info(
        "📡 非流式响应: %.1fs, raw=%d stripped=%d (think=%d, %d%%) | completion_tokens=%s",
        elapsed, len(raw), len(stripped), think_len,
        int(think_len / max(len(raw), 1) * 100),
        usage.get("completion_tokens", "?"),
    )
    return stripped


def _strip_think_blocks(text: str) -> str:
    """从完整文本中剥离所有 <think>…</think> 块。"""
    import re as _re
    # 处理可能跨行的 think 块
    result = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
    return result.strip()


class _ThinkStripper:
    """流式 <think> 块过滤器：逐 token 输入，自动分离推理和实际内容。

    Thinking 模型输出格式：<think>推理过程...</think>实际内容
    通过 on_thinking 回调流式透传推理过程，降低感知延迟。
    """

    def __init__(self, on_thinking=None) -> None:
        self._buffer = ""
        self._in_think = False
        self._think_tag_end = 0  # <think> 标签结束位置，用于后续提取推理内容
        self._on_thinking = on_thinking  # 回调: (text_chunk)

    def feed(self, token: str) -> str | None:
        """输入一个 token，返回剥离后的纯文本（可能为 None）。

        同时通过 on_thinking 回调逐段推送推理内容。
        """
        self._buffer += token
        output = ""

        if not self._in_think:
            # 检查是否进入 <think> 块
            tp = self._buffer.find("<think")
            if tp != -1:
                output = self._buffer[:tp]
                self._buffer = self._buffer[tp:]
                self._in_think = True
                # 记录 <think> 标签的结束位置
                tag_close = self._buffer.find(">")
                self._think_tag_end = tag_close + 1 if tag_close != -1 else len("<think>")
            else:
                # 防止 <think 被截断：保留最后 6 个字符
                safe = max(0, len(self._buffer) - 6)
                for i in range(6, 0, -1):
                    if "<think"[:i] == self._buffer[-i:]:
                        safe = max(0, len(self._buffer) - i)
                        break
                output = self._buffer[:safe]
                self._buffer = self._buffer[safe:]

        if self._in_think:
            # 检查是否退出 <think> 块
            ep = self._buffer.find("</think>")
            if ep != -1:
                # 提取推理内容（<think> 标签后、</think> 之前）
                reasoning = self._buffer[self._think_tag_end:ep]
                if reasoning and self._on_thinking:
                    self._on_thinking(reasoning)
                after = self._buffer[ep + len("</think>"):]
                self._buffer = after
                self._in_think = False
                self._think_tag_end = 0
                # 递归处理剩余内容
                if after:
                    rest = self.feed("")
                    if rest:
                        output = (output + rest) if output else rest
                return output if output else None
            else:
                # 仍在 think 内：流式推送推理内容（批量，降低回调频率）
                if self._think_tag_end > 0 and len(self._buffer) > self._think_tag_end:
                    safe_end = max(self._think_tag_end, len(self._buffer) - 7)
                    pending = safe_end - self._think_tag_end
                    # 批量推送：≥16 字符 或 缓冲区末尾有换行
                    if pending >= 16 or (pending > 0 and "\n" in self._buffer[self._think_tag_end:safe_end]):
                        chunk = self._buffer[self._think_tag_end:safe_end]
                        if chunk and self._on_thinking:
                            self._on_thinking(chunk)
                        self._buffer = self._buffer[:self._think_tag_end] + self._buffer[safe_end:]

        return output if output else None

    def flush(self) -> str | None:
        """流结束时清空缓冲区。"""
        if self._in_think:
            # 未闭合的 <think>：作为推理内容推送
            reasoning = self._buffer[self._think_tag_end:] if self._think_tag_end else self._buffer
            if reasoning and self._on_thinking:
                self._on_thinking(reasoning)
            logger.debug("_ThinkStripper: 未闭合 <think>，已推送 %d chars", len(reasoning))
            self._buffer = ""
            self._in_think = False
            self._think_tag_end = 0
            return None
        if self._buffer:
            out = self._buffer
            self._buffer = ""
            return out
        return None


def _chat_completion_stream(
    base_url: str, api_key: str, payload: dict, timeout: int = 300,
    progress: Progress | None = None,
) -> str:
    """流式 chat completion，返回完整响应文本；同时更新 progress（None 时禁用进度输出）。"""
    payload = {**payload, "stream": True}
    accumulated = ""
    accumulated_raw = ""  # 诊断：未过滤的原始内容
    token_count = 0
    t0 = time.perf_counter()
    ttft = None  # time-to-first-token (first non-think content token)
    pg = progress

    # 思考过程回调：更新进度显示，降低感知延迟
    thinking_shown = False
    # 正文开头的空行去除标记（模型常在 </think> 后输出 \n\n）
    strip_leading = True

    # 分块模式：streaming 内容写入当前分块条目而非主 partial
    def _chunk_target() -> dict | None:
        if progress is not None and 0 <= progress.current_chunk < len(progress.chunks):
            return progress.chunks[progress.current_chunk]
        return None

    def _on_thinking(text: str) -> None:
        nonlocal thinking_shown
        chunk = _chunk_target()
        if chunk is not None:
            # 分块模式：思考写入当前分块
            if not thinking_shown:
                chunk["status"] = "thinking"
                thinking_shown = True
            if len(chunk["text"]) < 3000:
                chunk["text"] += text
            return
        if not thinking_shown:
            # 用 stage 标记思考阶段（前端胶囊指示器显示），不再写入占位文字
            pg.stage = ProgressStage.THINKING
            pg.set("")
            thinking_shown = True
        # 思考内容流式输出（限制长度防止 UI 过载）
        current = pg.partial
        if len(current) < 2000:
            pg.append(text)

    # 推理内容解析模式：think_tag（vLLM，content 内联 <think> 标签）/ reasoning_content（标准 OpenAI 兼容，独立字段）
    reasoning_mode = llm_provider.agent_endpoint().reasoning_mode
    stripper = _ThinkStripper(on_thinking=_on_thinking) if reasoning_mode == "think_tag" else None

    finish_reason = None
    try:
        with _llm_semaphore:
            with httpx.stream(
                "POST", f"{base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            ) as resp:
                if resp.status_code != 200:
                    try:
                        body = resp.read().decode(errors="ignore")[:300]
                    except Exception:
                        body = "(无法读取响应体)"
                    raise RuntimeError(f"LLM 流式调用失败 HTTP {resp.status_code}: {body}")
                for line in resp.iter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                            # 追踪 finish_reason（诊断截断根因）
                            fr = (chunk.get("choices") or [{}])[0].get("finish_reason")
                            if fr:
                                finish_reason = fr
                            # 推理内容解析：think_tag 模式从 content 内联 <think> 标签提取（stripper）；
                            # reasoning_content 模式从独立字段读推理，content 已是纯正文（无 stripper）
                            if reasoning_mode == "reasoning_content":
                                rc = delta.get("reasoning_content", "")
                                if rc:
                                    _on_thinking(rc)
                            token = delta.get("content", "")
                            if token:
                                accumulated_raw += token  # 诊断：保留原始内容
                                # think_tag 模式分离 <think> 推理块；reasoning_content 模式直接用纯正文
                                stripped = stripper.feed(token) if stripper else token
                                if stripped:
                                    if strip_leading:
                                        stripped = stripped.lstrip()
                                        if stripped:
                                            strip_leading = False
                                if stripped:
                                    if ttft is None:
                                        ttft = time.perf_counter() - t0
                                        chunk = _chunk_target()
                                        if chunk is not None:
                                            # 分块模式：正文开始输出，清空思考内容只显示正文
                                            chunk["status"] = "summarizing"
                                            chunk["text"] = ""
                                            thinking_shown = False
                                        else:
                                            # 第一个正文 token 到达：清空思考内容，进入正文流式阶段
                                            pg.set("")
                                            pg.stage = ProgressStage.SUMMARY
                                            thinking_shown = False
                                    accumulated += stripped
                                    token_count += len(stripped)
                                    chunk = _chunk_target()
                                    if chunk is not None:
                                        if len(chunk["text"]) < 3000:
                                            chunk["text"] += stripped
                                    else:
                                        pg.append(stripped)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    except httpx.HTTPError as e:
        # 平台已知现象：多模态端点间歇性断开（siliconflow 401/ReadTimeout 自愈）。
        # 流已收到内容时保留降级返回——不把「已完成/半完成」的总结变成错误
        # （2026-08-14 用户反馈：总结已完整显示但批量工具报错）
        if not accumulated:
            raise
        logger.warning(
            "⚠️ 流式中断（finish_reason=%s，已收到 %d 字）: %s —— 保留已生成内容降级返回",  # noqa: E501
            finish_reason or "?", len(accumulated), e,
        )

    # 流结束：清空 stripper 残留，同时推送到 progress（reasoning_content 模式无 stripper）
    flushed = stripper.flush() if stripper else None
    if flushed and strip_leading:
        flushed = flushed.lstrip()
        if flushed:
            strip_leading = False
    flushed_len = len(flushed) if flushed else 0
    if flushed:
        if ttft is None:
            ttft = time.perf_counter() - t0
        accumulated += flushed
        token_count += len(flushed)
        chunk = _chunk_target()
        if chunk is not None:
            if len(chunk["text"]) < 3000:
                chunk["text"] += flushed
        else:
            pg.append(flushed)  # 修复：最后几个字符也要推送到前端

    elapsed = time.perf_counter() - t0
    raw_len = len(accumulated_raw)
    stripped_len = len(accumulated)
    logger.info(
        "📡 vLLM 响应: %d tokens / %.1fs (%.0f tok/s), TTFT %.2fs, "
        "raw=%d stripped=%d (flush=%d) chars | finish_reason=%s",
        token_count, elapsed, token_count / max(elapsed, 0.01),
        ttft or 0, raw_len, stripped_len, flushed_len, finish_reason or "?",
    )
    if finish_reason == "length":
        logger.warning("⚠️ vLLM 返回 finish_reason=length，输出被截断！可能需要增大 --max-num-batched-tokens")
    think_pct = int((1 - stripped_len / max(raw_len, 1)) * 100)
    logger.info(
        "  思考占 %d%%（%d chars）| 实际总结 %d chars",
        think_pct, raw_len - stripped_len, stripped_len,
    )
    return accumulated
