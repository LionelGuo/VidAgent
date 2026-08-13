import { create } from "zustand";

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

export interface VideoChapter {
  start: number;
  end: number;
  title: string;
  summary?: string;
}

/** 长视频分段总结的单段进度 */
export interface VideoChunk {
  index: number;
  total: number;
  time_start: number;
  time_end: number;
  status: "waiting" | "thinking" | "summarizing" | "done";
  text: string;
}

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
  task_status?: "downloading" | "analyzing" | "extracting" | "summarizing" | "summary" | "asr" | "thinking" | "chunking" | "merging" | "done" | "error";
  /** 下载进度 0-100 */
  download_progress?: number;
  /** 失败原因（task_status=error 时显示） */
  error?: string;
  /** 章节时间轴 */
  chapters?: VideoChapter[];
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
  updateProgress: (id: string, data: Partial<Pick<VideoInfo, "task_status" | "download_progress" | "error">>) => void;
  /** 设置章节列表 */
  setChapters: (id: string, chapters: VideoChapter[]) => void;
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
  setChapters: (id, chapters) =>
    set((s) => {
      const existing = s.videos[id];
      if (!existing) return s;
      return { videos: { ...s.videos, [id]: { ...existing, chapters } } };
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