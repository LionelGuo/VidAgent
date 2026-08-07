#!/usr/bin/env python3
"""诊断 LLM 流式输出速度：对比短上下文 vs 长上下文（模拟工具结果后的场景）。

测量两个核心指标：
  - TTFT (Time To First Token)：首个 token 延迟
  - TPS  (Tokens Per Second)：生成速度

用法:
  uv run python scripts/benchmark_speed.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vidagent.config import settings  # noqa: E402

# 模拟 extract_and_summarize 返回的总结文本（~1500 tokens 中文）
LONG_TOOL_RESULT = """
## 核心观点
1. 健身领域存在五大"害人"建议，这些看似合理实则错误的观点阻碍了健身者的进步。
2. 这些错误建议广泛流传于网络平台（如抖音、小红书等），甚至被一些"干货博主"传播。
3. 作者通过专业知识和研究数据驳斥了这些误区，并提供了更科学、更有效的训练理念。

## 主要内容梳理
### 错误建议一：深蹲时必须避免"屁股眨眼"
深蹲过程中骨盆向后倾斜，形似"眨眼"。传统观点认为骨盆后倾会导致腰椎屈曲，引发腰痛。
作者反驳：深蹲中骨盆后倾是离心阶段的短暂动作，持续时间短，不会对椎间盘造成过大压力。
而硬拉中的腰椎屈曲是向心阶段的主动抗阻动作，才是真正的风险来源。
结论：只要核心收紧，骨盆后倾是正常的生理现象。不要过度焦虑，关注整体动作控制与核心稳定。

### 错误建议二：有氧和力量训练不能同时做
传统观念认为有氧会消耗肌肉、升高皮质醇，导致肌肉流失。
作者引用研究反驳：实验对比显示力量+有氧组合增肌效果更好。
奥运摔跤、综合格斗、橄榄球运动员兼具高肌肉量与高强度有氧训练，说明二者可兼容。
建议：避免马拉松级长时间有氧；力量训练优先于有氧。

### 错误建议三：自然健身只能是"肌肉猴"或"纸包鸡"二选一
传统认知中的"自然上限FFMI=25"源自1995年研究，样本仅为健美冠军。
实际案例：铁血纳甘诺FFMI达28，中国力量举选手马明旺FFMI可达27。
结论：自然健身者的FFMI上限超过28。普通人通过科学训练达到FFMI23完全可行。

### 错误建议四：动作下放速度必须慢
IFBB Pro等职业选手强调慢速下放以保护肌腱。
2021年研究对比：快速下放组股四头肌远端肌肉增长（+5.5%）远超慢下组（+2.2%）。
建议：新手可尝试慢速下放学动作，进阶后正常控制节奏即可。

### 错误建议五："只跟自己比，不要跟别人比"
Comparison is the thief of joy 被广泛引用。
作者反驳：比较本身不是问题，错误处理比较信息才是问题。
两条路径：只跟自己比→进步缓慢，上限仅70%；跟别人比并复盘→达到95%潜力。
建议：选择第二条路，实现身体与心理的双重成长。
"""  # ~800 字中文 ≈ 1500 tokens


def measure_stream(base_url: str, api_key: str, model: str, label: str,
                   messages: list[dict], **kwargs) -> dict:
    """流式调用 LLM，测量 TTFT 和 TPS。"""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": True,
        **kwargs,
    }
    t_start = time.perf_counter()
    ttft = None
    token_count = 0
    content_chars = 0

    try:
        with httpx.stream(
            "POST", f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        ) as resp:
            if resp.status_code != 200:
                return {"label": label, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                import json
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                reasoning = delta.get("reasoning_content", "")

                if content:
                    if ttft is None:
                        ttft = time.perf_counter() - t_start
                    token_count += 1
                    content_chars += len(content)
                elif reasoning:
                    # reasoning tokens 也算（思考阶段）
                    if ttft is None:
                        ttft = time.perf_counter() - t_start
                    token_count += 1

    except Exception as e:
        return {"label": label, "error": str(e)}

    elapsed = time.perf_counter() - t_start
    return {
        "label": label,
        "elapsed_s": round(elapsed, 1),
        "ttft_s": round(ttft, 2) if ttft else None,
        "tokens": token_count,
        "content_chars": content_chars,
        "tps": round(token_count / (elapsed - (ttft or 0)), 1) if ttft and elapsed > (ttft or 0) else None,
    }


def main():
    base_url, api_key, model = settings.active_llm()
    if not api_key:
        print("❌ 未配置 API key")
        sys.exit(1)

    print(f"🔬 速度诊断：{model}")
    print(f"   端点: {base_url}")
    print()

    short_msg = [{"role": "user", "content": "用一句话介绍什么是深度学习。"}]
    long_msg = [
        {"role": "system", "content": "你是 VidAgent，一个视频总结助手。请基于工具返回的总结内容，用中文向用户简洁汇报。"},
        {"role": "user", "content": "总结B站热门第一条视频"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "extract_and_summarize", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": LONG_TOOL_RESULT},
    ]

    results = []

    for i in range(2):
        print(f"── 第 {i+1} 轮 ──")
        for messages, label in [(short_msg, "短上下文 (~50 tokens)"),
                                  (long_msg, "长上下文 (~2000 tokens, 模拟工具结果后)")]:
            r = measure_stream(base_url, api_key, model, label, messages)
            results.append(r)
            if r.get("error"):
                print(f"  ❌ [{label}] {r['error']}")
            else:
                print(f"  [{label}]")
                print(f"    TTFT: {r['ttft_s']}s | 总耗时: {r['elapsed_s']}s | "
                      f"tokens: {r['tokens']} | TPS: {r['tps']}")
        print()

    # 汇总
    short_ttft = [r["ttft_s"] for r in results if "短上下文" in r["label"] and r.get("ttft_s")]
    long_ttft = [r["ttft_s"] for r in results if "长上下文" in r["label"] and r.get("ttft_s")]
    short_tps = [r["tps"] for r in results if "短上下文" in r["label"] and r.get("tps")]
    long_tps = [r["tps"] for r in results if "长上下文" in r["label"] and r.get("tps")]

    print("═══ 汇总 ═══")
    if short_ttft and long_ttft:
        print(f"  TTFT  短: {sum(short_ttft)/len(short_ttft):.1f}s  →  长: {sum(long_ttft)/len(long_ttft):.1f}s  "
              f"(膨胀 {sum(long_ttft)/sum(short_ttft):.1f}x)")
    if short_tps and long_tps:
        print(f"  TPS   短: {sum(short_tps)/len(short_tps):.1f}  →  长: {sum(long_tps)/len(long_tps):.1f}  "
              f"(降至 {sum(long_tps)/sum(short_tps)*100:.0f}%)")

    print()
    if long_tps and short_tps and sum(long_tps) / sum(short_tps) < 0.5:
        print("⚠️ 长上下文 TPS 显著低于短上下文 → 瓶颈在上下文处理，不是模型本身速度")
        print("   本地 4090 部署可改善：KV cache 复用使 decode 阶段速度与上下文大小关系较小")
    elif long_ttft and short_ttft and sum(long_ttft) / sum(short_ttft) > 3:
        print("⚠️ TTFT 膨胀显著 → 模型在做内部推理（thinking），可能是 Omni 模型的固有特征")
        print("   尝试设置 LLM_EXTRA_BODY={\"enable_thinking\": false} 看是否能关闭思考阶段")
    else:
        print("📊 长/短上下文差异不大 → 瓶颈可能在 API 平台本身的吞吐限制")
        print("   本地 4090 部署可能获得更稳定的输出速度")


if __name__ == "__main__":
    main()
