"use client";

import { X, Maximize2, Minimize2, Play, Clock, Eye, User } from "lucide-react";
import { useRef } from "react";
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
        ) : (
          <div className="aspect-video bg-black flex items-center justify-center">
            {video?.local_path ? (
              <Play className="w-12 h-12 text-white/40" />
            ) : (
              <span className="text-white/40 text-sm">
                视频未下载 — 请先通过对话下载
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
            <h3 className="text-sm font-semibold">📊 AI 总结</h3>
            {video?.summary ? (
              <MarkdownRenderer>{video.summary}</MarkdownRenderer>
            ) : (
              <div className="rounded-lg border border-border bg-muted/50 p-4">
                <p className="text-sm text-muted-foreground">
                  {video
                    ? "该视频尚未总结。在对话中要求「总结」即可生成。"
                    : "选中视频后将在此显示详细信息。"}
                </p>
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
