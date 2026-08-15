// ⚠️ GENERATED FILE — 勿手改。
// 来源：scripts/gen-tool-schema.py（解析平台能力声明 supports_* / capability_notes、
// platforms/__init__.py 的 PLATFORM_MODULES / VIDEO_FIELDS、server/main.py 的
// DEFAULT_PLATFORM / DEFAULT_LIMIT）。
// 重新生成：python scripts/gen-tool-schema.py
// 一致性检查：python scripts/gen-tool-schema.py --check
// 本文件是工具 schema 结构化知识的单一来源：平台清单 / 能力矩阵 / 字段清单 /
// 默认值 / SYSTEM_PROMPT 知识片段（SYSTEM_KNOWLEDGE）。前端手写 describe 与
// prompts.ts 知识段引用本文件的生成片段，人工行为指导 prose 不在此处。

/** 平台清单（PLATFORM_MODULES 注册序）。 */
export const PLATFORMS = [
  "bilibili",
  "douyin",
  "kuaishou",
  "xiaohongshu",
  "youtube",
] as const;

export type PlatformName = (typeof PLATFORMS)[number];

/** 平台能力矩阵（来源：各平台类的 supports_* / capability_notes 声明）。 */
export const PLATFORM_CAPABILITIES: Record<
  PlatformName,
  { hot: boolean; search: boolean; creator: boolean; notes: Record<string, string> }
> = {
  "bilibili": { hot: true, search: true, creator: true, notes: {} },
  "douyin": { hot: true, search: true, creator: true, notes: {} },
  "kuaishou": { hot: false, search: true, creator: true, notes: {} },
  "xiaohongshu": { hot: false, search: true, creator: true, notes: {"search": "小红书搜索结果无时长信息（平台接口限制）", "creator": "小红书创作者视频列表无时长信息（平台接口限制）"} },
  "youtube": { hot: true, search: true, creator: true, notes: {"creator": "YouTube 创作者查询需后端配置 API key"} },
};

/** 统一视频字段清单（来源：platforms/__init__.py 的 VIDEO_FIELDS）。 */
export const VIDEO_FIELDS = [
  "video_id",
  "title",
  "desc",
  "publish_time",
  "duration",
  "duration_text",
  "video_url",
  "platform",
  "author",
  "view_count",
] as const;

/** 字段清单文本（检索工具 describe 的「每项含 …」片段，由 VIDEO_FIELDS 派生）。 */
export const FIELDS_TEXT = VIDEO_FIELDS.join("/");

/** 工具 API 默认值（来源：server/main.py 的 DEFAULT_PLATFORM / DEFAULT_LIMIT）。 */
export const DEFAULT_PLATFORM = "bilibili";
export const DEFAULT_LIMIT = 10;

/** 检索工具 describe 的平台句（由能力矩阵生成；行为指导 prose 留在调用点）。 */
const PLATFORM_DESCRIBE: Record<"hot" | "search" | "creator", string> = {
  hot: "平台：bilibili / douyin / youtube（kuaishou、xiaohongshu 不支持热榜）",
  search: "平台：bilibili / douyin / kuaishou / xiaohongshu / youtube（五平台均支持搜索；小红书搜索结果无时长信息（平台接口限制））",
  creator: "平台：bilibili / douyin / kuaishou / xiaohongshu / youtube（五平台均支持创作者查询；小红书创作者视频列表无时长信息（平台接口限制）；YouTube 创作者查询需后端配置 API key）",
};

export function describePlatformsFor(tool: keyof typeof PLATFORM_DESCRIBE): string {
  return PLATFORM_DESCRIBE[tool];
}

/** SYSTEM_PROMPT 知识片段（能力事实句，来源同上；R1Q7 知识通道原则：
 *  vllm 模式 relay 剥掉 tools 字段、describe 模型不可见，SYSTEM_PROMPT 是
 *  唯一两种 relay 模式都可见的知识通道）。prompts.ts 的【能力与知识】段
 *  拼装这些片段；行为指导散文手写在 prompts.ts，勿在此手写同义句。 */
export const SYSTEM_KNOWLEDGE = {
  platformsLine: "平台支持 bilibili、douyin、kuaishou、xiaohongshu、youtube；用户未指定时默认 bilibili。",
  searchCreatorLine: "search_videos 与 get_creator_videos 在五平台均可用（小红书创作者视频列表无时长信息（平台接口限制）；小红书搜索结果无时长信息（平台接口限制）；YouTube 创作者查询需后端配置 API key）。",
  hotLine: "get_hot_videos 仅 bilibili、douyin、youtube 支持热榜；kuaishou、xiaohongshu 没有热榜——不要对它们调用 get_hot_videos。用户想看这些平台的热门内容时：改用 search_videos（关键词贴近用户意图，不要照搬「热门」二字），并在回复开头说明该平台无热榜、以下为关键词搜索的结果（非官方榜单）。",
  fieldsLine: "三个检索工具返回的每个视频都含 video_id/title/desc/publish_time/duration/duration_text/video_url/platform/author/view_count（duration 为秒数，duration_text 如 \"12:34\"，publish_time 为发布时间）。",
} as const;
