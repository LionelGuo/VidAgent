import { streamText, extractReasoningMiddleware, wrapLanguageModel } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { z } from "zod";

// ---------------------------------------------------------------------------
// API 地址
// ---------------------------------------------------------------------------

const API_BASE =
  process.env.FASTAPI_URL?.replace(/\/v1\/?$/, "") || "http://127.0.0.1:8000";
const V1_BASE = `${API_BASE}/v1`;

const vidagent = createOpenAI({
  baseURL: V1_BASE,
  apiKey: "not-needed",
});

// ---------------------------------------------------------------------------
// 系统提示（复用 Agno agent 中验证过的 prompt）
// ---------------------------------------------------------------------------

const SYSTEM_PROMPT = `你是 VidAgent，一个自媒体视频采集与总结助手，通过调用工具完成用户的自然语言指令。

【可用工具】
- get_hot_videos(platform, limit, date_filter)：获取平台综合热门/榜单视频。
- search_videos(platform, keyword, limit, date_filter)：按关键词搜索视频。
- get_creator_videos(platform, creator, limit, date_filter)：获取指定创作者(UP主/YouTuber)的视频；
  creator 可为昵称(如「老番茄」，自动解析为 ID)或数字/字符串 ID。
- batch_summarize_videos(videos)：**【主要总结工具】** 批量并行下载+总结视频。
  传入视频对象数组，每项必须含 video_url 和 title。video_id 可选（后端自动从 URL 提取）。
  也可附带 desc/author/duration_text（推荐）。优先使用此工具。
- download_video(video_url, file_name)：仅下载不总结（单独使用时）。
- extract_and_summarize(local_path, metadata)：旧版单视频总结（batch_summarize_videos 的备用方案）。

【工具调用格式】
当需要使用工具时，请用以下格式输出（不要用 markdown 代码块包裹）：
<tool_call>
{"name": "工具名", "arguments": {...}}
</tool_call>

【检索工具选择（很重要）】
- 用户提到某位 UP 主/创作者人名（如「老番茄」「何同学」「罗翔」）→ 用
  get_creator_videos。creator 填人名即可，系统会自动解析为 ID。
- 用户用关键词描述想要的内容（如「Python教程」「搞笑视频」「游戏实况」）→ 用 search_videos。
  **注意：「搜索xx」「找xx教程」「关于xx的视频」这种都是在搜关键词，不要当成创作者去查。**
- 用户想看热门/榜单/「今天有什么火的」→ 用 get_hot_videos。

【筛选与下载（很重要）】
- 三个检索工具返回的每个视频都**已含** duration(秒) / duration_text(如"12:34") /
  view_count / publish_time。
- 当用户要求按「时长 / 播放量 / 日期」筛选时，**直接从返回结果里挑选符合条件的条目**，
  **不要**先 download_video 再判断时长。download_video 仅在用户明确要「总结/下载某个
  具体视频」时才调用。

【视频总结（最重要）】
- 用户要「总结」视频时，**必须调用 batch_summarize_videos**——无论几个视频。
  单视频也用它（传 1 个元素的数组），多视频传完整列表。
  从检索结果中提取每个视频的 video_url / video_id / title / desc / author / duration_text，
  组装为 videos 数组一次传入。不要先 download_video 再 extract_and_summarize。
- download_video 仅在用户**只想下载、不需要总结**时调用。
- extract_and_summarize 是旧版单视频工具，仅在 batch_summarize_videos 不可用时作为回退。

【其它】
- 平台支持 bilibili、youtube、douyin、kuaishou、xiaohongshu；用户未指定时默认 bilibili。
- **工具调用策略：收到工具结果后，先判断用户任务是否已完成。**
  如果用户仅需检索/列表（如"搜索xx教程，介绍一下"），检索完成后直接生成文本回复，不要继续下载或总结。
  不要在任务完成后调用无关工具。
- **date_filter 参数：默认不传。** 热榜/搜索本身反映当前热门内容，不需要按发布日期过滤。仅在用户明确说「只看今天/今日发布的」时才传 "today"。
- 工具返回 status=error 或抛异常时：简要说明原因；仍失败则如实告知，绝不编造内容。
- 全程中文；总结用 Markdown，分「核心观点」与「主要内容梳理」。
`;

// ---------------------------------------------------------------------------
// 助手
// ---------------------------------------------------------------------------

/** 裁剪视频元数据，避免 tool result 过大导致 AI SDK 消息校验失败 */
function trimVideoResults(data: any): any {
  if (data?.results && Array.isArray(data.results)) {
    data.results = data.results.map((v: any) => ({
      ...v,
      desc: v.desc ? v.desc.slice(0, 200) + (v.desc.length > 200 ? "…" : "") : "",
    }));
  }
  return data;
}

// ---------------------------------------------------------------------------
// 助手：等待异步总结任务完成
// ---------------------------------------------------------------------------

async function waitForSummarizeTask(taskId: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/tools/summarize/${taskId}/stream`);
  if (!res.ok) throw new Error(`总结失败: HTTP ${res.status}`);
  if (!res.body) throw new Error("无响应体");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      if (line === "data: [DONE]") break;

      try {
        const evt = JSON.parse(line.slice(6));
        if (evt.type === "done") {
          result = evt.result || result;
        } else if (evt.type === "error") {
          throw new Error(evt.message || "总结任务失败");
        }
      } catch (e) {
        if (e instanceof SyntaxError) continue;
        throw e;
      }
    }
  }

  return result || "总结完成（无文本内容）";
}

// ---------------------------------------------------------------------------
// POST /api/chat — AI SDK streamText + tool calling
// ---------------------------------------------------------------------------

export async function POST(req: Request) {
  const { messages } = await req.json();

  const result = streamText({
    model: wrapLanguageModel({
      model: vidagent("/root/autodl-tmp/Qwen3-Omni-Thinking-AWQ-4bit"),
      middleware: extractReasoningMiddleware({ tagName: "think" }),
    }),
    system: SYSTEM_PROMPT,
    messages,
    maxSteps: 10,
    onError: (err: any) => {
      const msg = err?.error?.message || err?.message || String(err);
      console.error("[streamText ERROR]", msg.slice(0, 1000));
      if (err?.error?.cause) console.error("[streamText CAUSE]", JSON.stringify(err.error.cause).slice(0, 500));
    },
    tools: {
      // ── 检索工具 ──
      get_hot_videos: {
        description:
          "获取平台综合热门视频榜单（热榜本身反映当前热度，不限发布日期）。返回视频列表，每项含 video_id/title/desc/duration/duration_text/video_url/platform/author/view_count。",
        parameters: z.object({
          platform: z.string().nullable().default("bilibili").describe("平台：bilibili / youtube"),
          limit: z.number().nullable().default(10).describe("返回条数上限"),
          date_filter: z.string().nullable().optional().describe("按发布日期过滤。通常不传（热榜已反映当前热度）。仅在用户明确要求'只看今天发布的'时才传 'today'"),
        }),
        execute: async ({ platform, limit, date_filter }) => {
          const p = new URLSearchParams({ platform: platform || "bilibili", limit: String(limit || 10) });
          if (date_filter) p.set("date_filter", date_filter);
          const res = await fetch(`${API_BASE}/api/tools/hot?${p}`);
          if (!res.ok) throw new Error(`获取热门失败: HTTP ${res.status}`);
          return trimVideoResults(await res.json());
        },
      },

      search_videos: {
        description:
          "按关键词搜索视频。返回视频列表，每项含 video_id/title/desc/duration/duration_text/video_url/platform/author/view_count。",
        parameters: z.object({
          platform: z.string().nullable().default("bilibili").describe("平台：bilibili / youtube"),
          keyword: z.string().describe("搜索关键词（必填）"),
          limit: z.number().nullable().default(10).describe("返回条数上限"),
          date_filter: z.string().nullable().optional().describe("时间过滤：today 表示仅当日"),
        }),
        execute: async ({ platform, keyword, limit, date_filter }) => {
          const p = new URLSearchParams({ platform: platform || "bilibili", keyword, limit: String(limit || 10) });
          if (date_filter) p.set("date_filter", date_filter);
          const res = await fetch(`${API_BASE}/api/tools/search?${p}`);
          if (!res.ok) throw new Error(`搜索失败: HTTP ${res.status}`);
          return trimVideoResults(await res.json());
        },
      },

      get_creator_videos: {
        description:
          "获取指定创作者（UP 主）的视频列表。creator 可为昵称（如「老番茄」）或数字 UID。返回视频列表，每项含 video_id/title/desc/duration/duration_text/video_url/platform/author/view_count。",
        parameters: z.object({
          platform: z.string().nullable().default("bilibili").describe("平台：bilibili / youtube"),
          creator: z.string().describe("创作者昵称或数字 UID（必填）"),
          limit: z.number().nullable().default(10).describe("返回条数上限"),
          date_filter: z.string().nullable().optional().describe("时间过滤：today 表示仅当日"),
        }),
        execute: async ({ platform, creator, limit, date_filter }) => {
          const p = new URLSearchParams({ platform: platform || "bilibili", creator, limit: String(limit || 10) });
          if (date_filter) p.set("date_filter", date_filter);
          const res = await fetch(`${API_BASE}/api/tools/creator?${p}`);
          if (!res.ok) throw new Error(`获取创作者视频失败: HTTP ${res.status}`);
          return trimVideoResults(await res.json());
        },
      },

      // ── 下载工具 ──
      download_video: {
        description:
          "下载无水印视频到本地。传入视频 URL（来自搜索/热榜结果的 video_url），下载到 workspace 目录。返回 local_path 供后续总结使用。",
        parameters: z.object({
          video_url: z.string().describe("视频播放页地址（来自检索结果的 video_url）"),
          file_name: z
            .string()
            .describe("保存文件名前缀，通常使用 video_id"),
        }),
        execute: async ({ video_url, file_name }) => {
          const res = await fetch(`${API_BASE}/api/tools/download`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ video_url, file_name }),
          });
          if (!res.ok) throw new Error(`下载失败: HTTP ${res.status}`);
          return res.json();
        },
      },

      // ── 批量总结工具（并行下载 + 总结多个视频）──
      batch_summarize_videos: {
        description:
          "【推荐】批量并行总结多个视频。传入视频列表，后端并行处理并等待全部完成（无需额外查询状态）。返回所有视频的完整总结文本。每个视频独立重试、独立错误。",
        parameters: z.object({
          videos: z
            .array(
              z.object({
                video_url: z.string().describe("视频播放页地址"),
                video_id: z.string().optional().describe("视频 ID（如 BVxxx，缺失时从 video_url 自动提取）"),
                title: z.string().describe("视频标题"),
                desc: z.string().nullable().optional().describe("视频简介"),
                author: z.string().nullable().optional().describe("作者/UP 主"),
                duration_text: z.string().nullable().optional().describe("时长文本"),
                duration: z.number().nullable().optional().describe("视频时长（秒）"),
                platform: z.string().nullable().optional().describe("平台（默认从 URL 自动检测）"),
              })
            )
            .describe("要总结的视频列表"),
        }),
        execute: async ({ videos }) => {
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 600_000); // 10 分钟超时
          const res = await fetch(`${API_BASE}/api/tools/batch-summarize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ videos }),
            signal: controller.signal,
          }).finally(() => clearTimeout(timeout));
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            throw new Error(`批量总结失败 HTTP ${res.status}: ${text.slice(0, 200)}`);
          }
          const data = await res.json();
          console.log("[batch_summarize_videos] response:", JSON.stringify(data).slice(0, 300));
          const results: any[] = data.results || [];
          const summaries = results
            .filter((r: any) => r.status === "done")
            .map((r: any) => `【${r.title}】\n${r.summary}`)
            .join("\n\n---\n\n");
          const errors = results.filter((r: any) => r.status === "error");
          const errorNote = errors.length > 0
            ? `\n\n⚠️ ${errors.length} 个视频处理失败：${errors.map((e: any) => e.video_id).join(", ")}`
            : "";
          return {
            batch_id: data.batch_id,
            done: summaries.length,
            failed: errors.length,
            result: summaries + errorNote,
            results: data.results,  // 保留原始 results 供前端 VideoStore 使用
          };
        },
      },

      // ── 总结工具（异步：启动 → 轮询 SSE → 返回结果）──
      extract_and_summarize: {
        description:
          "对本地视频生成结构化中文总结（Markdown）。传入 local_path（下载工具返回）和 metadata（检索工具返回的 title/desc 等），返回包含核心观点和主要内容梳理的 Markdown 总结。",
        parameters: z.object({
          local_path: z
            .string()
            .describe("本地视频文件路径（来自 download_video 返回的 local_path）"),
          metadata: z
            .object({
              title: z.string().optional().describe("视频标题"),
              desc: z.string().optional().describe("视频简介"),
              video_id: z.string().optional().describe("视频 ID"),
              platform: z.string().optional(),
              author: z.string().optional(),
              duration_text: z.string().optional(),
            })
            .optional()
            .describe("视频元数据（可选，含 title/desc/video_id 等）"),
        }),
        execute: async ({ local_path, metadata }) => {
          // 1. 启动总结任务
          const startRes = await fetch(`${API_BASE}/api/tools/summarize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ local_path, metadata }),
          });
          if (!startRes.ok) throw new Error(`启动总结失败: HTTP ${startRes.status}`);
          const { task_id } = await startRes.json();

          // 2. 等待完成
          const summary = await waitForSummarizeTask(task_id);
          return { status: "ok", summary, video_id: metadata?.video_id };
        },
      },
    },
  });

  return result.toDataStreamResponse({ sendReasoning: true });
}
