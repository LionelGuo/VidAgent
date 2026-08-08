"""llama.cpp 透明代理：修复 Qwen2.5-Coder 工具调用的 XML → OpenAI 格式转换。

llama.cpp 的 chat template 指示模型输出 <tool_call> XML，但 Qwen2.5-Coder
实际输出 <tools>/<json>/```json 等变体。此代理拦截响应，从 content 中
提取工具调用 JSON，重写为 OpenAI tool_calls 格式。

用法：
  python scripts/llama_proxy.py --port 8081 --backend http://127.0.0.1:8080

VidAgent .env 中将 OPENAI_BASE_URL 指向 http://127.0.0.1:8081/v1 即可。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ── 超轻量 HTTP 代理（无第三方依赖，仅 stdlib）─────────────────────────
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BACKEND = "http://127.0.0.1:8080"


def _make_tool_call(obj: dict, idx: int) -> dict | None:
    """从解析出的 JSON 对象构造 OpenAI tool_call。"""
    try:
        return {
            "id": f"call_{idx}",
            "type": "function",
            "function": {
                "name": obj["name"],
                "arguments": json.dumps(obj.get("arguments", {}), ensure_ascii=False),
            },
        }
    except (KeyError, TypeError):
        return None


def _try_parse_json(raw: str) -> dict | None:
    """尝试解析 JSON，处理 Jinja 转义残留的双花括号。"""
    # 先尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 处理 Jinja 残留: {{ → {, }} → }
    cleaned = raw.replace("{{", "{").replace("}}", "}")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def extract_tool_calls_from_content(content: str) -> list[dict] | None:
    """从 Qwen XML/JSON 格式的 content 中提取 OpenAI tool_calls。"""
    if not content:
        return None

    # ── 策略 A：XML 结构化格式 ──
    xml_patterns = [
        # <tool_call> / <tools> / <json> 包裹的 JSON
        (r"<tool_call>\s*(.*?)\s*</tool_call>", "json"),
        (r"<tools>\s*(.*?)\s*</tools>", "json"),
        (r"<json>\s*(.*?)\s*</json>", "json"),
        # <functionCall><name>X</name><arguments>{...}</arguments></functionCall>
        (r"<functionCall>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</functionCall>", "nested"),
        (r"<function-call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</function-call>", "nested"),
        (r'<function-call>\s*(\{.*?"name".*?\})\s*</function-call>', "json"),
    ]

    for pattern, kind in xml_patterns:
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            calls = []
            for match in matches:
                if kind == "nested":
                    name = match[0].strip()
                    args = _try_parse_json(match[1].strip())
                    if args is None:
                        continue
                    calls.append({
                        "id": f"call_{len(calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    })
                else:
                    obj = _try_parse_json(match.strip())
                    if obj and "name" in obj:
                        tc = _make_tool_call(obj, len(calls))
                        if tc:
                            calls.append(tc)
            if calls:
                return calls

    # ── 策略 B：代码块中的 JSON（```json / ``` / ```xml） ──
    code_fence_patterns = [
        r"```json\s*\n\s*(.*?)\s*\n\s*```",
        r'```\s*\n\s*(\{.*?"name".*?\})\s*\n\s*```',
        r"```xml\s*\n\s*<tools>\s*(.*?)\s*</tools>\s*\n\s*```",
        r"```xml\s*\n\s*<functionCall>.*?</functionCall>\s*\n\s*```",
    ]
    for pattern in code_fence_patterns:
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            calls = []
            for raw in matches:
                obj = _try_parse_json(raw.strip())
                if obj and "name" in obj:
                    tc = _make_tool_call(obj, len(calls))
                    if tc:
                        calls.append(tc)
            if calls:
                return calls

    # ── 策略 C：裸 JSON（大括号计数匹配，处理任意嵌套 arguments）──
    return _extract_json_tool_calls(content)


def _extract_json_tool_calls(content: str) -> list[dict] | None:
    """用大括号计数扫描 content，提取所有 {"name":...,"arguments":...} JSON 对象。"""
    # 找到所有 "name" 出现的位置作为候选起点
    candidates = []
    for m in re.finditer(r'"name"\s*:\s*"([^"]+)"', content):
        name_val = m.group(1)
        before = content[:m.start()]
        # 从 "name" 往前找最近的 {
        brace_start = before.rfind('{')
        if brace_start < 0:
            continue
        # 跳过双花括号的外层 {{...}} → 从第二个 { 开始
        if brace_start > 0 and content[brace_start - 1:brace_start] == '{':
            brace_start -= 1

        # 从 brace_start 向前扫描，计数大括号
        # 统一按单个 { } 计数（{{ }} 视为两个独立括号，Jinja artifact 同样处理）
        depth = 0
        i = brace_start
        while i < len(content):
            if content[i] == '{':
                depth += 1
                i += 1
            elif content[i] == '}':
                depth -= 1
                i += 1
            else:
                i += 1
            if depth == 0:
                candidates.append((brace_start, i, name_val))
                break

    if not candidates:
        return None

    calls = []
    seen_positions = set()
    for start, end, fallback_name in candidates:
        if start in seen_positions:
            continue
        seen_positions.add(start)
        raw = content[start:end]
        obj = _try_parse_json(raw)
        if obj and "name" in obj:
            tc = _make_tool_call(obj, len(calls))
            if tc:
                calls.append(tc)

    return calls if calls else None


def clean_content(content: str) -> str | None:
    """移除工具调用 XML/JSON 块，返回纯文本；若全是工具调用则返回 None。"""
    cleaned = content
    # 先移除 XML 标签 + 代码块（有明确边界）
    patterns = [
        r"<tool_call>\s*.*?\s*</tool_call>",
        r"<tools>\s*.*?\s*</tools>",
        r"<json>\s*.*?\s*</json>",
        r"<functionCall>.*?</functionCall>",
        r"<function-call>.*?</function-call>",
        r"```xml\s*\n\s*<tools>.*?</tools>\s*\n\s*```",
        r"```xml\s*\n\s*<functionCall>.*?</functionCall>\s*\n\s*```",
        r'```json\s*\n\s*\{.*?"name".*?\}\s*\n\s*```',
        r'```\s*\n\s*\{.*?"name".*?\}\s*\n\s*```',
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.DOTALL)

    # 移除裸 JSON 工具调用（大括号计数）
    tc = _extract_json_tool_calls(cleaned)
    if tc:
        # 重新提取并移除
        for m in re.finditer(r'"name"\s*:\s*"([^"]+)"', cleaned):
            before = cleaned[:m.start()]
            brace_start = before.rfind('{')
            if brace_start < 0:
                continue
            if brace_start > 0 and cleaned[brace_start - 1:brace_start] == '{':
                brace_start -= 1
            depth = 0
            i = brace_start
            while i < len(cleaned):
                if cleaned[i] == '{':
                    depth += 1; i += 1
                elif cleaned[i] == '}':
                    depth -= 1; i += 1
                else:
                    i += 1
                if depth == 0:
                    cleaned = cleaned[:brace_start] + cleaned[i:]
                    break

    cleaned = cleaned.strip()
    return cleaned if cleaned else None


def _process_tool_calls(resp_body: bytes) -> bytes:
    """非流式响应后处理：从 content 提取 <tool_call> XML → tool_calls。"""
    try:
        data = json.loads(resp_body)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content", "")
        if content and not msg.get("tool_calls"):
            tool_calls = extract_tool_calls_from_content(content)
            if tool_calls:
                msg["tool_calls"] = tool_calls
                msg["content"] = clean_content(content)
                choice["finish_reason"] = "tool_calls"
                return json.dumps(data, ensure_ascii=False).encode()
    except json.JSONDecodeError:
        pass
    return resp_body


class ProxyHandler(BaseHTTPRequestHandler):
    """将请求转发到 llama.cpp，后处理含工具调用的响应。"""
    protocol_version = "HTTP/1.1"  # OpenAI SDK 要求 HTTP/1.1 才处理 SSE 流

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # 保存原始 stream 标志
        was_stream = False
        try:
            was_stream = json.loads(body).get("stream", False)
        except (json.JSONDecodeError, AttributeError):
            pass

        # 清理 Agno 发送但 vLLM 不兼容的字段
        has_tools = False
        if self.path.endswith("/chat/completions") and body:
            try:
                req_data = json.loads(body)
                req_data.pop("stream_options", None)
                has_tools = bool(req_data.get("tools"))
                # 有 tools 时强制非流式：
                # --enable-auto-tool-choice 的语法约束只影响流式输出
                if has_tools and req_data.get("stream"):
                    req_data["stream"] = False
                body = json.dumps(req_data, ensure_ascii=False).encode()
            except (json.JSONDecodeError, AttributeError):
                pass

        url = f"{BACKEND}{self.path}"
        req = Request(url, data=body, method="POST")
        for key, val in self.headers.items():
            if key.lower() in ("host", "content-length", "connection"):
                continue
            req.add_header(key, val)

        # 流式透传：仅纯文本（无 tools）时保持 stream=true 实时转发
        # 有 tools 时 vLLM 的 --enable-auto-tool-choice 语法约束会破坏流式输出
        # → 强制非流式 + 后处理提取 tool_calls
        if was_stream and self.path.endswith("/chat/completions") and not has_tools:
            self._proxy_stream(body)
            return

        # 非流式：直接转发
        try:
            resp = urlopen(req, timeout=300)
            resp_body = resp.read()
            resp_headers = dict(resp.headers)
        except HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
            return
        except URLError as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Backend unreachable: {e.reason}"}).encode())
            return

        # 后处理：从 content 提取 tool_calls
        if self.path.endswith("/chat/completions") and resp.getcode() == 200:
            resp_body = _process_tool_calls(resp_body)

        # 有 tools 的流式请求 → 非流式获取 → 包装为 SSE 返回给 Agno
        if was_stream and has_tools and self.path.endswith("/chat/completions"):
            self._send_as_sse(resp_body)
        else:
            self.send_response(resp.getcode())
            for key, val in resp_headers.items():
                if key.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(resp_body)

    def _proxy_stream(self, body: bytes) -> None:
        """流式透传：从 vLLM 读 SSE chunks，纯文本实时转发，tool_call 则回退提取。"""
        body_json = json.loads(body)
        body_json["stream"] = True
        body = json.dumps(body_json, ensure_ascii=False).encode()

        url = f"{BACKEND}{self.path}"
        req = Request(url, data=body, method="POST")
        for key, val in self.headers.items():
            if key.lower() in ("host", "content-length", "connection"):
                continue
            req.add_header(key, val)

        try:
            resp = urlopen(req, timeout=300)
        except HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
            return
        except URLError as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Backend unreachable: {e.reason}"}).encode())
            return

        # 流式处理：先缓冲少量 chunk 判断是否为 tool_call，纯文本则立即透传
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # Phase 1: 缓冲直到确定类型（首段内容收集 ~20 chars 即可判断）
        lookahead: list[bytes] = []
        accumulated = ""
        is_tool_call = False
        decided = False

        for line in resp:
            if not decided:
                lookahead.append(line)
                if line.startswith(b"data: ") and line not in (b"data: [DONE]\n", b"data: [DONE]"):
                    try:
                        chunk = json.loads(line[6:])
                        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                        token = delta.get("content", "")
                        if token:
                            accumulated += token
                            if len(accumulated) >= 20 or line == b"data: [DONE]\n":
                                # 判断：以 < 或 { 开头 → tool_call
                                stripped = accumulated.lstrip()
                                is_tool_call = stripped.startswith(("<", "{"))
                                decided = True
                                if not is_tool_call:
                                    # 纯文本 → 输出缓冲的 chunks，后续实时透传
                                    for buf_line in lookahead:
                                        self.wfile.write(buf_line)
                                        self.wfile.flush()
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
            else:
                # Phase 2: 已决定类型
                if is_tool_call:
                    lookahead.append(line)  # 继续缓冲
                else:
                    self.wfile.write(line)  # 实时透传
                    self.wfile.flush()

        # 若为 tool_call → 从缓冲中提取并重写
        if is_tool_call and accumulated:
            # 从所有缓冲 chunk 重建完整内容（不仅是前 20 chars 的 lookahead）
            full_content = ""
            for line in lookahead:
                if line.startswith(b"data: ") and line not in (b"data: [DONE]\n", b"data: [DONE]"):
                    try:
                        chunk = json.loads(line[6:])
                        token = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
                        if token:
                            full_content += token
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
            tool_calls = extract_tool_calls_from_content(full_content)
            if tool_calls:
                self._send_tool_call_body(lookahead, tool_calls, full_content)
                return
            # 提取失败：回退为纯文本透传
            for buf_line in lookahead:
                self.wfile.write(buf_line)
                self.wfile.flush()

    def _send_as_sse(self, resp_body: bytes) -> None:
        """将非流式 JSON 响应包装为 SSE chunks。"""
        try:
            data = json.loads(resp_body)
        except json.JSONDecodeError:
            self.send_response(500); self.end_headers(); return

        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        model = data.get("model", "")
        finish = (data.get("choices") or [{}])[0].get("finish_reason", "stop")
        created = data.get("created", 0)
        cid = data.get("id", "")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # role
        self.wfile.write(f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]},ensure_ascii=False)}\n\n".encode())
        # content
        if content:
            for i in range(0, len(content), 8):
                self.wfile.write(f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{'content':content[i:i+8]},'finish_reason':None}]},ensure_ascii=False)}\n\n".encode())
        # tool_calls
        for idx, tc in enumerate(tool_calls):
            self.wfile.write(f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{'tool_calls':[{**tc,'index':idx}]},'finish_reason':None}]},ensure_ascii=False)}\n\n".encode())
        # final
        self.wfile.write(f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}},ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _send_tool_call_body(
        self, original_chunks: list[bytes], tool_calls: list[dict], raw_content: str
    ) -> None:
        """在 HTTP body 中发送带 tool_calls 的 SSE 流（headers 已发送）。"""
        chat_id = ""
        model = ""
        created = 0
        for chunk in original_chunks:
            if chunk.startswith(b"data: ") and chunk not in (b"data: [DONE]\n", b"data: [DONE]"):
                try:
                    c = json.loads(chunk[6:])
                    chat_id = c.get("id", chat_id) or chat_id
                    model = c.get("model", model) or model
                    created = c.get("created", created) or created
                    break
                except json.JSONDecodeError:
                    pass

        cleaned = clean_content(raw_content) or ""
        finish_reason = "tool_calls"

        self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n".encode())
        if cleaned:
            for i in range(0, len(cleaned), 8):
                self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'content': cleaned[i:i+8]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n".encode())
        for idx, tc in enumerate(tool_calls):
            self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{**tc, 'index': idx}]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish_reason}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        url = f"{BACKEND}{self.path}"
        req = Request(url, method="GET")
        for key, val in self.headers.items():
            if key.lower() in ("host", "connection"):
                continue
            req.add_header(key, val)
        try:
            resp = urlopen(req, timeout=30)
            self.send_response(resp.getcode())
            for key, val in dict(resp.headers).items():
                if key.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(resp.read())
        except HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
        except URLError as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Backend unreachable: {e.reason}"}).encode())

    def log_message(self, format, *args):
        """抑制默认日志；仅在工具调用转换时输出。"""
        if "tool_calls" in str(args):
            sys.stderr.write(f"[llama_proxy] {args[0]}\n")


def main():
    import argparse
    p = argparse.ArgumentParser(description="llama.cpp → OpenAI tool_calls proxy")
    p.add_argument("--port", type=int, default=8081, help="Proxy listen port (default: 8081)")
    p.add_argument("--backend", default="http://127.0.0.1:8080", help="llama.cpp server URL")
    args = p.parse_args()

    global BACKEND
    BACKEND = args.backend.rstrip("/")

    server = HTTPServer(("127.0.0.1", args.port), ProxyHandler)
    print(f"[llama_proxy] {BACKEND} → http://127.0.0.1:{args.port}")
    print(f"[llama_proxy] 工具调用 XML→OpenAI 转换已启用")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[llama_proxy] 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
