"use client";

import { X, Maximize2, Minimize2 } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// DetailPanel — 右侧详情面板容器
//
// 当前为 Phase 1 骨架（展示 videoId + 占位内容）。
// Phase 3 将集成：
//   - Vidstack 视频播放器
//   - AI 总结全文（Markdown 渲染）
//   - React Flow 交互式思维导图
// ---------------------------------------------------------------------------

interface DetailPanelProps {
  videoId: string;
  onClose: () => void;
}

export function DetailPanel({ videoId, onClose }: DetailPanelProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={cn(
        "h-full flex flex-col bg-card border-l border-border",
        "transition-all duration-300",
        expanded ? "fixed inset-0 z-50" : ""
      )}
    >
      {/* 工具栏 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <h2 className="text-sm font-medium truncate">
          📺 视频详情
        </h2>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1.5 rounded-md hover:bg-muted transition-colors"
            title={expanded ? "缩小" : "全屏"}
          >
            {expanded ? (
              <Minimize2 className="w-4 h-4" />
            ) : (
              <Maximize2 className="w-4 h-4" />
            )}
          </button>
          <button
            onClick={onClose}
            className="p-1.5 rounded-md hover:bg-muted transition-colors"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto">
        {/* 播放器占位（Phase 3 → Vidstack） */}
        <div className="aspect-video bg-black flex items-center justify-center">
          <span className="text-white/40 text-sm">
            🎬 视频播放器 (Phase 3)
          </span>
        </div>

        {/* 总结占位 */}
        <div className="p-6 space-y-4">
          <div className="space-y-2">
            <h3 className="text-sm font-semibold">📊 AI 总结</h3>
            <div className="rounded-lg border border-border bg-muted/50 p-4">
              <p className="text-sm text-muted-foreground">
                选中视频后将在此显示 AI 总结内容。
              </p>
              <p className="text-xs text-muted-foreground/60 mt-2">
                Video ID: {videoId}
              </p>
            </div>
          </div>

          {/* 思维导图占位（Phase 3 → React Flow） */}
          <div className="space-y-2">
            <h3 className="text-sm font-semibold">🧠 内容脉络</h3>
            <div className="rounded-lg border border-border bg-muted/50 p-8 flex items-center justify-center">
              <span className="text-sm text-muted-foreground">
                交互式思维导图 (Phase 3)
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
