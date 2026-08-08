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
