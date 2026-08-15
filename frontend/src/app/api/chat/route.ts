import { streamText, extractReasoningMiddleware, wrapLanguageModel } from "ai";
import { createOpenAI } from "@ai-sdk/openai";

import { SYSTEM_PROMPT } from "@/lib/agent/prompts";
import { API_BASE, TOOLS } from "@/lib/agent/tools";

// ---------------------------------------------------------------------------
// POST /api/chat — AI SDK streamText + tool calling
// ---------------------------------------------------------------------------
// 提示词与工具定义集中在 src/lib/agent/（#8 提示词集中管理）：
// prompts.ts 散文 system prompt；tools.ts 工具 schema（结构化知识来自
// lib/tool-schema.ts 的 codegen 生成片段）。本文件只做模型装配与流式响应。

const vidagent = createOpenAI({
  baseURL: `${API_BASE}/v1`,
  apiKey: "not-needed",
});

export async function POST(req: Request) {
  const { messages } = await req.json();

  const result = streamText({
    model: wrapLanguageModel({
      // 模型名由后端 relay 从 LLM_MODEL 注入，前端发占位符即可
      model: vidagent("vidagent-agent"),
      middleware: extractReasoningMiddleware({ tagName: "think" }),
    }),
    system: SYSTEM_PROMPT,
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
