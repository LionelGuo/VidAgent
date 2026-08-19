import { create } from "zustand";

import { SUMMARY_STAGES, type SummaryStage, type TaskStatus } from "./sse-events";

// ---------------------------------------------------------------------------
// Layout Store — 驱动 chat / detail panel 混合布局
// ---------------------------------------------------------------------------

interface LayoutState {
  /** 当前选中的视频 ID（null = 仅显示聊天面板） */
  selectedVideoId: string | null;
  selectVideo: (id: string | null) => void;
}

export const useLayoutStore = create<LayoutState>((set) => ({
  selectedVideoId: null,
  selectVideo: (id) => set({ selectedVideoId: id }),
}));

// ---------------------------------------------------------------------------
// Video Store — 视频元数据（从工具结果中提取，驱动 DetailPanel）
// ---------------------------------------------------------------------------

/** 长视频分段总结的单段进度 */
export interface VideoChunk {
  index: number;
  total: number;
  time_start: number;
  time_end: number;
  status: "waiting" | "thinking" | "summarizing" | "done";
  text: string;
}

/** 落库的任务状态（task_status）：wire 阶段去掉空闲哨兵 ""（被 if(data.stage)
 *  拦截）与瞬态 downloaded（ChatView 拦截改写为 extracting），再加 SSE 终态
 *  （TaskStatus 去掉 processing——processing 从不落库，任务由阶段事件驱动）。
 *  由后端枚举生成的 SummaryStage/TaskStatus 派生——后端加新值时此处自动收编。 */
export type StoredTaskStatus =
  | Exclude<SummaryStage, "" | "downloaded">
  | Exclude<TaskStatus, "processing">
  /** 前端本地态：B站分P视频未指定分P（分P待选，等模型问用户后以 -pN 新卡继续） */
  | "multi_part";

/** 落库状态中「总结进行中」的集合（ChatView 状态指示器）：排除下载相关阶段。
 *  由生成的 SUMMARY_STAGES 派生——后端新增阶段自动落入「进行中」分支。 */
export const SUMMARIZING_STAGES = SUMMARY_STAGES.filter(
  (s) => s !== "downloaded" && s !== "downloading",
) as StoredTaskStatus[];

/** 落库状态中「工作中」的集合（DetailPanel 胶囊）：同上再排除 summary（最终总结态不算工作中）。 */
export const WORKING_STAGES = SUMMARY_STAGES.filter(
  (s) => s !== "downloaded" && s !== "downloading" && s !== "summary",
) as StoredTaskStatus[];

export interface VideoInfo {
  video_id: string;
  title: string;
  desc: string;
  author: string;
  duration_text: string;
  video_url: string;
  view_count: number;
  platform?: string;
  publish_time?: string;
  /** 视频时长（秒），用于时间轴渲染 */
  duration?: number;
  /** 下载后填充 */
  local_path?: string;
  /** 总结后填充 */
  summary?: string;
  /** 任务状态 */
  task_status?: StoredTaskStatus;
  /** 分P总数（task_status=multi_part 时显示「分P待选」提示用） */
  total_parts?: number;
  /** 下载进度 0-100 */
  download_progress?: number;
  /** 失败原因（task_status=error 时显示） */
  error?: string;
  /** 长视频分段总结进度 */
  chunks?: VideoChunk[];
}

interface VideoStore {
  videos: Record<string, VideoInfo>;
  /** 批量写入检索结果 */
  upsertResults: (results: VideoInfo[]) => void;
  /** 设置下载路径 */
  setLocalPath: (id: string, path: string) => void;
  /** 设置总结内容 */
  setSummary: (id: string, summary: string) => void;
  /** 更新进度字段 */
  updateProgress: (id: string, data: Partial<Pick<VideoInfo, "task_status" | "download_progress" | "error" | "total_parts">>) => void;
  /** 设置视频时长 */
  setDuration: (id: string, duration: number) => void;
  /** 设置分段总结进度 */
  setChunks: (id: string, chunks: VideoChunk[]) => void;
}

export const useVideoStore = create<VideoStore>((set) => ({
  videos: {},
  upsertResults: (results) =>
    set((s) => {
      const next = { ...s.videos };
      for (const v of results) {
        if (!v.video_id) continue;
        const existing = next[v.video_id];
        // 合并：新值覆盖已有值，但保留非空字段（防 batch args 空 desc 覆盖搜索数据）
        next[v.video_id] = {
          ...existing,
          ...Object.fromEntries(
            Object.entries(v).filter(([_, val]) => val !== "" && val !== null && val !== undefined)
          ),
          // video_id 必须保留（filter 可能过滤空字符串，但 video_id 不应为空）
          video_id: v.video_id,
        };
      }
      return { videos: next };
    }),
  setLocalPath: (id, path) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, local_path: path } } };
    }),
  setSummary: (id, summary) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, summary } } };
    }),
  updateProgress: (id, data) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, ...data } } };
    }),
  setDuration: (id, duration) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, duration } } };
    }),
  setChunks: (id, chunks) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, chunks } } };
    }),
}));