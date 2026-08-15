// ---------------------------------------------------------------------------
// 主 Agent 工具定义模块（#8 提示词集中管理）
// ---------------------------------------------------------------------------
//
// 结构化知识（平台句/字段清单/默认值）引用 lib/tool-schema.ts 的生成片段
// （codegen 单一来源：scripts/gen-tool-schema.py，CI --check 守漂移）；
// 行为指导 prose（「【推荐】」「应引导用户改用搜索」等）在此人工维护。
//
// 行为变化标注（#8）：
// - B1：creator 平台句由生成器重建（含「YouTube 创作者查询需后端配置
//   API key」备注，如实化原「五平台均可用」表述）。
// - B2：三处检索 describe 字段清单改为生成片段（补漏列的 publish_time）。
// - B3：batch_summarize_videos 的 videos 元素删除 duration 字段——
//   后端 BatchVideoItem 从不读取（pydantic 静默丢弃），属死字段。

import { z } from "zod";

import {
  DEFAULT_LIMIT,
  DEFAULT_PLATFORM,
  FIELDS_TEXT,
  describePlatformsFor,
} from "@/lib/tool-schema";

// ---------------------------------------------------------------------------
// API 地址（route.ts 的模型端点也从此取 API_BASE）
// ---------------------------------------------------------------------------

export const API_BASE =
  process.env.FASTAPI_URL?.replace(/\/v1\/?$/, "") || "http://127.0.0.1:8000";

// ---------------------------------------------------------------------------
// 工具参数类型（execute 独立成模块后需显式标注；zod 解析后的形状）
// ---------------------------------------------------------------------------

type PlatformLimitArgs = {
  platform: string | null;
  limit: number | null;
  date_filter?: string | null;
};

/** 检索工具通用查询串：平台/条数默认值 + 可选日期过滤 + 附加键。 */
function buildQuery(args: PlatformLimitArgs, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams({
    platform: args.platform || DEFAULT_PLATFORM,
    limit: String(args.limit || DEFAULT_LIMIT),
    ...extra,
  });
  if (args.date_filter) p.set("date_filter", args.date_filter);
  return `?${p}`;
}

interface SummaryMetadata {
  title?: string;
  desc?: string;
  video_id?: string;
  platform?: string;
  author?: string;
  duration_text?: string;
}

/** 批量总结的单个视频条目（execute 参数类型 = zod 声明的推导） */
const videoItemSchema = z.object({
  video_url: z.string().describe("视频播放页地址"),
  video_id: z.string().optional().describe("视频 ID（如 BVxxx，缺失时从 video_url 自动提取）"),
  title: z.string().describe("视频标题"),
  desc: z.string().nullable().optional().describe("视频简介"),
  author: z.string().nullable().optional().describe("作者/UP 主"),
  duration_text: z.string().nullable().optional().describe("时长文本"),
  platform: z.string().nullable().optional().describe("平台（默认从 URL 自动检测）"),
});

type BatchVideo = z.infer<typeof videoItemSchema>;

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

/** 等待异步总结任务完成（轮询 SSE 流，取 done 事件的结果文本） */
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
// 工具定义（streamText 的 tools 参数）
// ---------------------------------------------------------------------------

export const TOOLS = {
  // ── 检索工具 ──
  get_hot_videos: {
    description:
      "获取平台综合热门视频榜单（热榜本身反映当前热度，不限发布日期）。返回视频列表，每项含 " +
      FIELDS_TEXT +
      "。注意：kuaishou、xiaohongshu 不支持热榜，返回结果含 message 提示，应引导用户改用搜索。",
    parameters: z.object({
      platform: z
        .string()
        .nullable()
        .default(DEFAULT_PLATFORM)
        .describe("平台：" + describePlatformsFor("hot")),
      limit: z.number().int().min(1).max(50).nullable().default(DEFAULT_LIMIT).describe("返回条数上限（1-50）"),
      date_filter: z
        .string()
        .nullable()
        .optional()
        .describe("按发布日期过滤。通常不传（热榜已反映当前热度）。仅在用户明确要求'只看今天发布的'时才传 'today'"),
    }),
    execute: async (args: PlatformLimitArgs) => {
      const res = await fetch(`${API_BASE}/api/tools/hot${buildQuery(args)}`);
      if (!res.ok) throw new Error(`获取热门失败: HTTP ${res.status}`);
      return trimVideoResults(await res.json());
    },
  },

  search_videos: {
    description:
      "按关键词搜索视频。返回视频列表，每项含 " +
      FIELDS_TEXT +
      "。注意：xiaohongshu 搜索结果没有时长信息（duration=0，平台限制，属正常现象）。",
    parameters: z.object({
      platform: z
        .string()
        .nullable()
        .default(DEFAULT_PLATFORM)
        .describe("平台：" + describePlatformsFor("search")),
      keyword: z.string().describe("搜索关键词（必填）"),
      limit: z.number().int().min(1).max(50).nullable().default(DEFAULT_LIMIT).describe("返回条数上限（1-50）"),
      date_filter: z.string().nullable().optional().describe("时间过滤：today 表示仅当日"),
    }),
    execute: async (args: PlatformLimitArgs & { keyword: string }) => {
      const res = await fetch(`${API_BASE}/api/tools/search${buildQuery(args, { keyword: args.keyword })}`);
      if (!res.ok) throw new Error(`搜索失败: HTTP ${res.status}`);
      return trimVideoResults(await res.json());
    },
  },

  get_creator_videos: {
    description:
      "获取指定创作者（UP 主）的视频列表。creator 可为昵称（如「老番茄」）或数字 UID。返回视频列表，每项含 " +
      FIELDS_TEXT +
      "。",
    parameters: z.object({
      platform: z
        .string()
        .nullable()
        .default(DEFAULT_PLATFORM)
        .describe("平台：" + describePlatformsFor("creator")),
      creator: z.string().describe("创作者昵称或数字 UID（必填）"),
      limit: z.number().int().min(1).max(50).nullable().default(DEFAULT_LIMIT).describe("返回条数上限（1-50）"),
      date_filter: z.string().nullable().optional().describe("时间过滤：today 表示仅当日"),
    }),
    execute: async (args: PlatformLimitArgs & { creator: string }) => {
      const res = await fetch(`${API_BASE}/api/tools/creator${buildQuery(args, { creator: args.creator })}`);
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
    execute: async ({ video_url, file_name }: { video_url: string; file_name: string }) => {
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
      videos: z.array(videoItemSchema).describe("要总结的视频列表"),
    }),
    execute: async ({ videos }: { videos: BatchVideo[] }) => {
      const controller = new AbortController();
      // 30 分钟超时（多长视频批量任务耗时长）；SUMMARY_TIMEOUT_MS 可覆盖（分发用户的自救开关）
      const timeoutMs = parseInt(process.env.SUMMARY_TIMEOUT_MS || "") || 1_800_000;
      const timeout = setTimeout(() => controller.abort(), timeoutMs);
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
      console.log("[api] batch_summarize response:", JSON.stringify(data).slice(0, 300));
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
    execute: async ({ local_path, metadata }: { local_path: string; metadata?: SummaryMetadata }) => {
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
};
