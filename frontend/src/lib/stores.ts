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
  /** 下载后填充 */
  local_path?: string;
  /** 总结后填充 */
  summary?: string;
  /** 任务状态 */
  task_status?: "downloading" | "extracting" | "summarizing" | "done" | "error";
  /** 下载进度 0-100 */
  download_progress?: number;
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
  updateProgress: (id: string, data: Partial<Pick<VideoInfo, "task_status" | "download_progress">>) => void;
}

export const useVideoStore = create<VideoStore>((set) => ({
  videos: {},
  upsertResults: (results) =>
    set((s) => {
      const next = { ...s.videos };
      for (const v of results) {
        if (!v.video_id) continue;
        next[v.video_id] = { ...next[v.video_id], ...v };
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
}));

// ---------------------------------------------------------------------------
// Tool Progress Store — 补充 AI SDK toolInvocations，跟踪长时间工具进度
// ---------------------------------------------------------------------------

interface TaskInfo {
  status: "processing" | "done" | "error";
  partial: string;
  result: string | null;
}

interface ToolProgressState {
  tasks: Record<string, TaskInfo>;
  updateTask: (id: string, data: Partial<TaskInfo>) => void;
  removeTask: (id: string) => void;
}

export const useToolProgressStore = create<ToolProgressState>((set) => ({
  tasks: {},
  updateTask: (id, data) =>
    set((s) => ({
      tasks: { ...s.tasks, [id]: { ...s.tasks[id], ...data } },
    })),
  removeTask: (id) =>
    set((s) => {
      const { [id]: _, ...rest } = s.tasks;
      return { tasks: rest };
    }),
}));
