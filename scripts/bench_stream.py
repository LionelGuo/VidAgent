"""直接调用 vLLM-Omni，流式打印到终端，测量真实生成速度。"""
import httpx
import sys
import time
import json

BASE = "https://u805822-pmbb-c2867d57.westd.seetacloud.com:8443/v1"
MODEL = "/root/autodl-tmp/Qwen3-Omni-30B-AWQ"

payload = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "写一篇800字的文章，介绍人工智能的发展历史，从1950年代一直讲到2020年代。"}],
    "max_tokens": 1200,
    "temperature": 0.7,
    "stream": True,
}

t0 = time.perf_counter()
first_token_time = None
total_tokens = 0

with httpx.stream("POST", f"{BASE}/chat/completions", json=payload, timeout=120) as resp:
    for line in resp.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            token = delta.get("content", "")
            if token:
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                total_tokens += len(token)
                sys.stdout.write(token)
                sys.stdout.flush()

elapsed = time.perf_counter() - t0
ttft = (first_token_time - t0) if first_token_time else 0

print(f"\n\n{'='*50}")
print(f"总耗时:   {elapsed:.1f}s")
print(f"首 token: {ttft:.2f}s")
print(f"Token 数: ~{total_tokens}")
print(f"速率:     {total_tokens / max(elapsed - ttft, 0.01):.0f} tok/s")
