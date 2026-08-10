"use client";

import { X, Maximize2, Minimize2, Play, Clock, Eye, User } from "lucide-react";
import { useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { useVideoStore } from "@/lib/stores";
import { apiBaseUrl } from "@/lib/api";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";

// ---------------------------------------------------------------------------
// DetailPanel — 右侧视频详情卡片
//
// 从 VideoStore 读取视频元数据、下载路径、总结内容、章节时间轴。
// 卡片形态：大圆角 + 阴影 + 浮起感，而非半个网页的扁平分割。
// ---------------------------------------------------------------------------

interface DetailPanelProps {
  videoId: string;
  onClose: () => void;
}

export function DetailPanel({ videoId, onClose }: DetailPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const video = useVideoStore((s) => s.videos[videoId]);

  // 从 local_path 构造视频 URL
  const videoSrc = video?.local_path
    ? `${apiBaseUrl}/workspace/${video.local_path.replace(/^.*[\\/]/, "")}`
    : null;

  return (
    <div
      className={cn(
        "h-full flex flex-col bg-card rounded-2xl shadow-xl border border-border",
        "overflow-hidden transition-all duration-300",
        expanded && "fixed inset-4 z-50 rounded-2xl shadow-2xl"
      )}
    >
      {/* ── 工具栏 ── */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0 bg-card">
        <h2 className="text-sm font-semibold truncate max-w-[70%]">
          {video?.title ?? "📺 视频详情"}
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

        {/* 章节导航 */}
        {video?.chapters && video.chapters.length > 0 && (
          <div className="px-5 py-3 border-b border-border">
            <h3 className="text-xs font-semibold mb-2">📑 章节</h3>
            <div className="flex flex-wrap gap-1.5">
              {video.chapters.map((ch, i) => (
                <button
                  key={i}
                  onClick={() => {
                    const vid = videoRef.current;
                    if (vid) {
                      // fastSeek 比直接赋值 currentTime 更快（跳过解码）
                      if (typeof vid.fastSeek === "function") {
                        vid.fastSeek(ch.start);
                      } else {
                        vid.currentTime = ch.start;
                      }
                    }
                  }}
                  className="text-xs px-2.5 py-1 rounded-full bg-muted hover:bg-primary/10
                             hover:text-primary transition-colors"
                  title={`${formatTime(ch.start)} - ${formatTime(ch.end)}`}
                >
                  {ch.title}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* 元数据 */}
        <div className="px-5 py-4 space-y-3">
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

          {/* 内容脉络（章节时间轴的 V1 形态） */}
          <div className="space-y-2">
            <h3 className="text-sm font-semibold">🧠 内容脉络</h3>
            {video?.chapters && video.chapters.length > 0 ? (
              <div className="space-y-1.5">
                {video.chapters.map((ch, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 rounded-lg border border-border bg-muted/30 px-3 py-2
                               hover:bg-muted/50 transition-colors cursor-pointer"
                    onClick={() => {
                      const vid = videoRef.current;
                      if (vid) {
                        if (typeof vid.fastSeek === "function") {
                          vid.fastSeek(ch.start);
                        } else {
                          vid.currentTime = ch.start;
                        }
                      }
                    }}
                  >
                    <span className="text-xs font-mono text-muted-foreground shrink-0">
                      {formatTime(ch.start)}
                    </span>
                    <span className="text-sm truncate">{ch.title}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded-lg border border-border bg-muted/50 p-8 flex items-center justify-center">
                <span className="text-sm text-muted-foreground">
                  {video?.summary
                    ? "该视频无章节数据"
                    : "总结完成后将自动生成章节时间轴"}
                </span>
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