import { streamText, extractReasoningMiddleware, wrapLanguageModel } from "ai";
import { createOpenAI } from "@ai-sdk/openai";

import { buildSystemPrompt } from "@/lib/agent/prompts";
import { API_BASE, TOOLS } from "@/lib/agent/tools";

// ---------------------------------------------------------------------------
// POST /api/chat — AI SDK streamText + tool calling
// ---------------------------------------------------------------------------
// 提示词与工具定义集中在 src/lib/agent/（#8 提示词集中管理）：
// prompts.ts 散文 system prompt（三层结构 + 按 relay 模式条件拼装）；
// tools.ts 工具 schema（结构化知识来自 lib/tool-schema.ts 的 codegen 生成
// 片段）。本文件只做模型装配与流式响应。

const vidagent = createOpenAI({
  baseURL: `${API_BASE}/v1`,
  apiKey: "not-needed",
});

// Provider 元数据（批次⑤ B6）：XML 工具调用协议段仅 xml relay 模式拼入
// SYSTEM_PROMPT（transparent 模式原生 function calling，该段是噪声）。
// 模块级缓存（进程生命周期取一次）；取失败兜底 = 包含（与既有行为一致）。
let _includeToolCallProtocol: boolean | null = null;
async function shouldIncludeToolCallProtocol(): Promise<boolean> {
  if (_includeToolCallProtocol !== null) return _includeToolCallProtocol;
  try {
    const res = await fetch(`${API_BASE}/api/meta`, { cache: "no-store" });
    if (res.ok) {
      const meta = await res.json();
      if (typeof meta.relay_mode === "string") {
        _includeToolCallProtocol = meta.relay_mode !== "transparent";
        return _includeToolCallProtocol;
      }
    }
  } catch {
    // 网络/后端未就绪：兜底包含
  }
  _includeToolCallProtocol = true;
  return true;
}

export async function POST(req: Request) {
  const { messages } = await req.json();
  const includeToolCallProtocol = await shouldIncludeToolCallProtocol();

  const result = streamText({
    model: wrapLanguageModel({
      // 模型名由后端 relay 从 LLM_MODEL 注入，前端发占位符即可
      model: vidagent("vidagent-agent"),
      middleware: extractReasoningMiddleware({ tagName: "think" }),
    }),
    system: buildSystemPrompt({ includeToolCallProtocol }),
    messages,
    maxSteps: 10,
    onError: (err: any) => {
      const msg = err?.error?.message || err?.message || String(err);
      console.error("[api] streamText error:", msg.slice(0, 1000));
      if (err?.error?.cause) console.error("[api] streamText cause:", JSON.stringify(err.error.cause).slice(0, 500));
    },
    tools: TOOLS,
  });

  return result.toDataStreamResponse({ sendReasoning: true });
}
