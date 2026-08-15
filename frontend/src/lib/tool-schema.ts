// ⚠️ GENERATED FILE — 勿手改。
// 来源：scripts/gen-tool-schema.py（解析平台能力声明 supports_* / capability_notes、
// platforms/__init__.py 的 PLATFORM_MODULES / VIDEO_FIELDS、server/main.py 的
// DEFAULT_PLATFORM / DEFAULT_LIMIT）。
// 重新生成：python scripts/gen-tool-schema.py
// 一致性检查：python scripts/gen-tool-schema.py --check
// 本文件是工具 schema 结构化知识的单一来源：平台清单 / 能力矩阵 / 字段清单 /
// 默认值。前端手写 describe 引用本文件的生成片段，人工行为指导 prose 不在此处。

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
  "xiaohongshu": { hot: false, search: true, creator: true, notes: {} },
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

/** 字段清单文本（检索工具 describe 的「每项含 …」片段）。 */
export const FIELDS_TEXT = "video_id/title/desc/publish_time/duration/duration_text/video_url/platform/author/view_count";

/** 工具 API 默认值（来源：server/main.py 的 DEFAULT_PLATFORM / DEFAULT_LIMIT）。 */
export const DEFAULT_PLATFORM = "bilibili";
export const DEFAULT_LIMIT = 10;

/** 检索工具 describe 的平台句（由能力矩阵生成；行为指导 prose 留在调用点）。 */
const PLATFORM_DESCRIBE: Record<"hot" | "search" | "creator", string> = {
  hot: "平台：bilibili / douyin / youtube（kuaishou、xiaohongshu 不支持热榜）",
  search: "平台：bilibili / douyin / kuaishou / xiaohongshu / youtube（五平台均支持搜索）",
  creator: "平台：bilibili / douyin / kuaishou / xiaohongshu / youtube（五平台均支持创作者查询；YouTube 创作者查询需后端配置 API key）",
};

export function describePlatformsFor(tool: keyof typeof PLATFORM_DESCRIBE): string {
  return PLATFORM_DESCRIBE[tool];
}
