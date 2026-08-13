"use client";

import { X, Maximize2, Minimize2, Play, Clock, Eye, User, Loader2 } from "lucide-react";
import { useRef } from "react";
import { cn } from "@/lib/utils";
import { useVideoStore } from "@/lib/stores";
import { apiBaseUrl } from "@/lib/api";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";

// ---------------------------------------------------------------------------
// DetailPanel — 视频详情卡片（内容层）
//
// 自身不管理定位——定位由父级 fixed overlay 通过 CSS transition 统一驱动。
// expanded / onToggleFullscreen 由父级传入，DetailPanel 只负责 UI 呈现。
// ---------------------------------------------------------------------------

interface DetailPanelProps {
  videoId: string;
  expanded: boolean;
  onToggleFullscreen: () => void;
  onClose: () => void;
}

export function DetailPanel({ videoId, expanded, onToggleFullscreen, onClose }: DetailPanelProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const video = useVideoStore((s) => s.videos[videoId]);

  // 从 local_path 构造视频 URL
  const videoSrc = video?.local_path
    ? `${apiBaseUrl}/workspace/${video.local_path.replace(/^.*[\\/]/, "")}`
    : null;

  // 总结状态胶囊指示器：由 task_status 阶段事件驱动（后端显式推送）
  const taskStatus = video?.task_status;
  const isWorking =
    taskStatus === "extracting" ||
    taskStatus === "summarizing" ||
    taskStatus === "analyzing" ||
    taskStatus === "asr" ||
    taskStatus === "thinking" ||
    taskStatus === "chunking" ||
    taskStatus === "merging";
  const pillLabels: Record<string, string> = {
    thinking: "思考中",
    chunking: "分段总结中",
    merging: "合并总结中",
  };
  const summaryPill = {
    label: pillLabels[taskStatus ?? ""] ?? (isWorking ? "正在总结" : "内容总结"),
    working: isWorking,
  };

  // 分段总结框：仅在分段/合并阶段展示；
  // 合并阶段开始思考后（taskStatus 变为 thinking/summary）隐藏，只显示整体总结
  const showChunks =
    video?.chunks &&
    video.chunks.length > 0 &&
    (taskStatus === "chunking" || taskStatus === "merging");

  return (
    <div className="h-full flex flex-col bg-card rounded-2xl shadow-xl border border-border overflow-hidden">
      {/* ── 工具栏 ── */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0 bg-card">
        <h2 className="text-sm font-semibold truncate max-w-[70%]">
          {video?.title ?? "📺 视频详情"}
        </h2>
        <div className="flex items-center gap-1">
          <button
            onClick={onToggleFullscreen}
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

      {/* ── 内容区 ── */}
      <div className="flex-1 overflow-y-auto">
        {/* 视频播放器 */}
        {videoSrc ? (
          <div className="bg-black">
            <video
              ref={videoRef}
              src={videoSrc}
              controls
              preload="metadata"
              className="w-full aspect-video"
            >
              您的浏览器不支持视频播放。
            </video>
          </div>
        ) : video?.task_status === "downloading" ? (
          <div className="aspect-video bg-black flex flex-col items-center justify-center gap-2">
            <Loader2 className="w-8 h-8 text-white/60 animate-spin" />
            <span className="text-white/60 text-sm">
              下载中 {video.download_progress ?? 0}%
            </span>
          </div>
        ) : (
          <div className="aspect-video bg-black flex items-center justify-center">
            {video?.local_path ? (
              <Play className="w-12 h-12 text-white/40" />
            ) : (
              <span className="text-white/40 text-sm">
                视频未下载
              </span>
            )}
          </div>
        )}

        {/* 元数据 */}
        <div className="px-5 py-5 space-y-3">
          {video && (
            <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
              {video.author && (
                <span className="inline-flex items-center gap-1">
                  <User className="w-3.5 h-3.5" />
                  {video.author}
                </span>
              )}
              {video.duration_text && (
                <span className="inline-flex items-center gap-1">
                  <Clock className="w-3.5 h-3.5" />
                  {video.duration_text}
                </span>
              )}
              {video.view_count > 0 && (
                <span className="inline-flex items-center gap-1">
                  <Eye className="w-3.5 h-3.5" />
                  {formatCount(video.view_count)}
                </span>
              )}
            </div>
          )}

          {/* 视频简介 */}
          {video?.desc && (
            <p className="text-xs text-muted-foreground leading-relaxed line-clamp-3">
              {video.desc}
            </p>
          )}

          {/* AI 总结 */}
          <div className="space-y-2">
            {video?.task_status !== "downloading" && (
              <div
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium",
                  summaryPill.working
                    ? "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                    : "bg-muted text-muted-foreground"
                )}
              >
                {summaryPill.working && (
                  <Loader2 className="w-3 h-3 animate-spin" />
                )}
                {summaryPill.label}
              </div>
            )}
            {video?.summary && (
              // 思考阶段：内容降低不透明度，与正文视觉区分
              <div className={cn(taskStatus === "thinking" && "opacity-60")}>
                <MarkdownRenderer>{video.summary}</MarkdownRenderer>
              </div>
            )}

            {/* 分段总结框（长视频分块处理时逐段显示） */}
            {showChunks && (
              <div className="space-y-2 pt-1">
                {video!.chunks!.map((ch) => (
                  <div
                    key={ch.index}
                    className={cn(
                      "rounded-lg border border-border px-3 py-2 space-y-1.5",
                      ch.status === "waiting" && "opacity-50",
                      (ch.status === "thinking" || ch.status === "summarizing") &&
                        "border-blue-200 bg-blue-50/50"
                    )}
                  >
                    {/* 左上角：分段 + 时间范围；右侧：状态 */}
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-medium text-muted-foreground">
                        段落 {ch.index}/{ch.total} · {formatTime(ch.time_start)}–
                        {formatTime(ch.time_end)}
                      </span>
                      {ch.status !== "done" && (
                        <span
                          className={cn(
                            "inline-flex items-center gap-1 text-xs shrink-0",
                            ch.status === "waiting"
                              ? "text-muted-foreground"
                              : "text-blue-600"
                          )}
                        >
                          <Loader2 className="w-3 h-3 animate-spin" />
                          {ch.status === "waiting"
                            ? "等待中"
                            : ch.status === "thinking"
                              ? "思考中"
                              : "总结中"}
                        </span>
                      )}
                    </div>
                    {/* 段落的思考 + 总结内容；思考阶段限高，只显示最新几行 */}
                    {ch.text && (
                      <div
                        className={cn(
                          ch.status === "thinking" &&
                            "max-h-24 overflow-hidden flex items-end"
                        )}
                      >
                        <p
                          className={cn(
                            "text-xs text-muted-foreground whitespace-pre-wrap leading-relaxed",
                            ch.status === "thinking" && "opacity-60"
                          )}
                        >
                          {ch.text}
                        </p>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

function formatCount(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}
