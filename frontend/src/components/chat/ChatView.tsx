"use client";

import { memo, useEffect, useRef, type MutableRefObject, type ReactNode } from "react";
import { type Message } from "@ai-sdk/react";
import { cn } from "@/lib/utils";
import { useLayoutStore, useVideoStore, type VideoInfo } from "@/lib/stores";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";
import { streamSummaryByVideo, type SSEController } from "@/lib/api";
import {
  CheckCircle,
  Loader2,
  Search,
  Flame,
  User,
  Download,
  FileText,
} from "lucide-react";

// ---------------------------------------------------------------------------
// 工具名称 → 图标 + 中文标签映射
// ---------------------------------------------------------------------------

const TOOL_META: Record<string, { icon: ReactNode; label: string }> = {
  get_hot_videos: { icon: <Flame className="w-3.5 h-3.5" />, label: "热门" },
  search_videos: { icon: <Search className="w-3.5 h-3.5" />, label: "搜索" },
  get_creator_videos: { icon: <User className="w-3.5 h-3.5" />, label: "创作者" },
  download_video: { icon: <Download className="w-3.5 h-3.5" />, label: "下载" },
  extract_and_summarize: { icon: <FileText className="w-3.5 h-3.5" />, label: "总结" },
};

/** 返回检索结果的工具名集合 */
const SEARCH_TOOLS = new Set(["get_hot_videos", "search_videos", "get_creator_videos"]);

// ---------------------------------------------------------------------------
// ToolBadge — 工具调用状态指示器
// ---------------------------------------------------------------------------

export function ToolBadge({
  toolName,
  state,
}: {
  toolName: string;
  state: "pending" | "running" | "done" | "error";
}) {
  const meta = TOOL_META[toolName] ?? { icon: null, label: toolName };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        "transition-colors duration-200",
        state === "pending" && "bg-muted text-muted-foreground",
        state === "running" && "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
        state === "done" && "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
        state === "error" && "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
      )}
    >
      {state === "running" ? (
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
      ) : state === "done" ? (
        <CheckCircle className="w-3.5 h-3.5" />
      ) : state === "pending" ? (
        <span className="w-3.5 h-3.5 flex items-center justify-center">
          <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse-dot" />
        </span>
      ) : null}
      {meta.icon && state !== "running" && state !== "pending" && meta.icon}
      {meta.label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// VideoCard — 内嵌视频小结卡片（点击触发 DetailPanel）
// ---------------------------------------------------------------------------

export const VideoCard = memo(function VideoCard({
  videoId,
  title,
  author,
  duration,
  summary,
}: {
  videoId: string;
  title: string;
  author?: string;
  duration?: string;
  summary?: string;
}) {
  const selectVideo = useLayoutStore((s) => s.selectVideo);

  return (
    <button
      onClick={() => selectVideo(videoId)}
      className="w-full text-left rounded-xl border border-border bg-card p-4
                 hover:shadow-md hover:border-primary/20 transition-all duration-200
                 active:scale-[0.99] cursor-pointer"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-medium truncate">{title}</h4>
          <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
            {author && <span>{author}</span>}
            {duration && <span>{duration}</span>}
          </div>
          {summary && (
            <p className="text-xs text-muted-foreground mt-2 line-clamp-3">
              {summary}
            </p>
          )}
        </div>
        <div className="shrink-0 w-24 h-16 rounded-lg bg-muted flex items-center justify-center">
          <span className="text-2xl">🎬</span>
        </div>
      </div>
    </button>
  );
});

// ---------------------------------------------------------------------------
// ChatMessage — 单条消息渲染（memo：相同 message 引用时跳过渲染）
// ---------------------------------------------------------------------------

const ChatMessage = memo(function ChatMessage({
  message,
  onVideoClick,
}: {
  message: Message;
  onVideoClick: (id: string) => void;
}) {
  const isUser = message.role === "user";

  return (
    <div className={cn("mb-6 animate-fade-in", isUser ? "flex justify-end" : "")}>
      <div
        className={cn(
          "max-w-[85%]",
          isUser
            ? "bg-primary text-primary-foreground rounded-2xl rounded-br-md px-4 py-2.5"
            : "text-foreground"
        )}
      >
        {isUser ? (
          <p className="text-sm whitespace-pre-wrap">{message.content}</p>
        ) : (
          <AssistantContent message={message} onVideoClick={onVideoClick} />
        )}
      </div>
    </div>
  );
}, (prev, next) => prev.message === next.message);

// ---------------------------------------------------------------------------
// 从工具调用结果中提取视频数据
// ---------------------------------------------------------------------------

function extractVideoResults(toolInvocations: any[]): VideoInfo[] {
  const videos: VideoInfo[] = [];

  for (const ti of toolInvocations) {
    if (ti.state !== "result") continue;

    // 检索工具 → 提取 results[]
    if (SEARCH_TOOLS.has(ti.toolName)) {
      const results = ti.result?.results ?? [];
      for (const v of results) {
        if (v.video_id) {
          videos.push({
            video_id: v.video_id,
            title: v.title ?? "未知标题",
            desc: v.desc ?? "",
            author: v.author ?? "",
            duration_text: v.duration_text ?? "",
            duration: v.duration,  // 数字时长（秒）
            video_url: v.video_url ?? "",
            view_count: v.view_count ?? 0,
            platform: v.platform,
            publish_time: v.publish_time,
          });
        }
      }
    }

    // 批量总结 → 提取 videos[] 元数据 + 完成时写入 summary
    if (ti.toolName === "batch_summarize_videos") {
      const batchVideos: any[] = ti.args?.videos ?? [];
      for (const v of batchVideos) {
        const vid = v.video_id || (v.video_url?.match(/BV[\w]+/)?.[0]);
        if (vid) {
          videos.push({
            video_id: vid,
            title: v.title ?? "未知标题",
            desc: v.desc ?? "",
            author: v.author ?? "",
            duration_text: v.duration_text ?? "",
            duration: v.duration,  // 数字时长（秒）
            video_url: v.video_url ?? "",
            view_count: 0,
          });
        }
      }
      // 批量完成 → 写入 summary + local_path + task_status
      if (ti.state === "result" && ti.result?.results) {
        for (const r of ti.result.results) {
          if (!r.video_id) continue;
          if (r.status === "done" && r.summary) {
            useVideoStore.getState().setSummary(r.video_id, r.summary);
          }
          if (r.local_path) {
            useVideoStore.getState().setLocalPath(r.video_id, r.local_path);
          }
          useVideoStore.getState().updateProgress(r.video_id, {
            task_status: r.status === "done" ? "done" : "error",
          });
        }
      }
    }

    // 下载工具 → 关联 local_path
    if (ti.toolName === "download_video" && ti.result?.local_path) {
      const localPath = ti.result.local_path;
      // 从路径中提取 video_id（格式如 workspace/BVxxx.mp4）
      const match = localPath.match(/(BV[\w]+)/);
      if (match) {
        const vid = match[1];
        useVideoStore.getState().setLocalPath(vid, localPath);
      }
    }

    // 总结工具 → 关联 summary（通过 video_id 精确匹配）
    if (ti.toolName === "extract_and_summarize" && ti.result?.summary) {
      const vid = ti.result.video_id;
      if (vid) {
        useVideoStore.getState().setSummary(vid, ti.result.summary);
      }
    }
  }

  return videos;
}

// ---------------------------------------------------------------------------
// AssistantContent — 渲染 AI 回复（文本 + tool badges + video cards）
// ---------------------------------------------------------------------------

function AssistantContent({
  message,
  onVideoClick,
}: {
  message: Message;
  onVideoClick: (id: string) => void;
}) {
  const toolInvocations = (message as any).toolInvocations ?? [];
  const reasoning = (message as any).reasoning as string | undefined;

  // 从所有检索工具中提取视频卡片
  const videoCards = toolInvocations
    .filter((ti: any) => ti.state === "result" && SEARCH_TOOLS.has(ti.toolName))
    .flatMap((ti: any) => {
      const results = ti.result?.results ?? [];
      return results.map((v: any, i: number) => (
        <VideoCard
          key={v.video_id ?? `${ti.toolName}-${i}`}
          videoId={v.video_id ?? String(i)}
          title={v.title ?? "未知标题"}
          author={v.author}
          duration={v.duration_text}
          summary={v.desc}
        />
      ));
    });

  return (
    <div className="space-y-3">
      {/* 工具状态行 */}
      {toolInvocations.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {toolInvocations.map((ti: any, i: number) => {
            const state =
              ti.state === "result"
                ? "done"
                : ti.state === "call"
                  ? "running"
                  : "pending";
            return (
              <ToolBadge
                key={`${ti.toolCallId}-${i}`}
                toolName={ti.toolName ?? "工具"}
                state={state}
              />
            );
          })}
        </div>
      )}

      {/* 推理过程（可折叠，模型思考内容） */}
      {reasoning && (
        <details className="group" open={!message.content}>
          <summary className="text-xs text-muted-foreground/60 cursor-pointer hover:text-muted-foreground transition-colors select-none">
            🤔 思考过程{reasoning.length > 0 ? `（${reasoning.length} 字）` : ""}
          </summary>
          <div className="mt-2 text-xs text-muted-foreground/50 whitespace-pre-wrap leading-relaxed border-l-2 border-muted pl-3">
            {reasoning.slice(0, 2000)}
            {reasoning.length > 2000 && <span className="text-muted-foreground/30">…（已截断）</span>}
          </div>
        </details>
      )}

      {/* 文本内容（Markdown 渲染） */}
      {message.content && (
        <MarkdownRenderer>{message.content}</MarkdownRenderer>
      )}

      {/* 视频卡片 */}
      {videoCards.length > 0 && (
        <div className="space-y-2 pt-1">{videoCards}</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SSE 连接辅助
// ---------------------------------------------------------------------------

function _connectSSE(
  videoId: string,
  activeStreamsRef: MutableRefObject<Map<string, SSEController>>,
): SSEController {
  return streamSummaryByVideo(
    videoId,
    (data) => {
      const store = useVideoStore.getState();
      // 下载完成 → 立即可播放
      if (data.stage === "downloaded" && data.local_path) {
        store.setLocalPath(videoId, data.local_path);
        store.updateProgress(videoId, { task_status: "extracting" });
        return;
      }
      // stage 事件
      if (data.stage) {
        store.updateProgress(videoId, { task_status: data.stage, download_progress: data.download_pct });
      }
      // 流式文本
      if (data.message) {
        store.setSummary(videoId, data.message);
      }
    },
    (result, extra) => {
      const store = useVideoStore.getState();
      store.setSummary(videoId, result);
      if (extra?.local_path) store.setLocalPath(videoId, extra.local_path);
      if (extra?.chapters && extra.chapters.length > 0) {
        store.setChapters(videoId, extra.chapters);
      }
      store.updateProgress(videoId, { task_status: "done" });
      activeStreamsRef.current.delete(videoId);
    },
    (err) => {
      console.warn("总结 SSE 错误:", err.message);
      activeStreamsRef.current.delete(videoId);
    },
  );
}

function _closeSSE(
  videoId: string | undefined,
  activeStreamsRef: MutableRefObject<Map<string, SSEController>>,
) {
  if (!videoId) return;
  const ctrl = activeStreamsRef.current.get(videoId);
  if (ctrl) {
    ctrl.close();
    activeStreamsRef.current.delete(videoId);
  }
}

// ---------------------------------------------------------------------------
// ChatView — 消息列表 + 工具结果 → VideoStore 同步
// ---------------------------------------------------------------------------

export function ChatView({
  messages,
  status,
  onVideoClick,
}: {
  messages: Message[];
  status: string;
  onVideoClick: (id: string) => void;
}) {
  // 工具结果 → VideoStore 同步
  useEffect(() => {
    for (const msg of messages) {
      if (msg.role !== "assistant") continue;
      const toolInvocations = (msg as any).toolInvocations ?? [];
      const videos = extractVideoResults(toolInvocations);
      if (videos.length > 0) {
        useVideoStore.getState().upsertResults(videos);
      }
    }
  }, [messages]);

  // 总结流式进度 → VideoStore（fetch-SSE 独立连接，无自动重连）
  const activeStreamsRef = useRef<Map<string, SSEController>>(new Map());

  useEffect(() => {
    for (const msg of messages) {
      if (msg.role !== "assistant") continue;
      const toolInvocations: any[] = (msg as any).toolInvocations ?? [];

      for (const ti of toolInvocations) {
        // ── extract_and_summarize（单视频）──
        if (ti.toolName === "extract_and_summarize") {
          const videoId: string | undefined = ti.args?.metadata?.video_id;
          if (!videoId) continue;

          if (ti.state === "call" && !activeStreamsRef.current.has(videoId)) {
            activeStreamsRef.current.set(videoId, _connectSSE(videoId, activeStreamsRef));
          }
          if (ti.state === "result") {
            _closeSSE(videoId, activeStreamsRef);
          }
        }

        // ── batch_summarize_videos（批量）──
        if (ti.toolName === "batch_summarize_videos") {
          const batchVideos: any[] = ti.args?.videos ?? [];

          if (ti.state === "call") {
            // 预填 VideoStore 元数据（DetailPanel 必须在总结开始前就知道视频信息）
            const videoInfos: VideoInfo[] = [];
            for (const v of batchVideos) {
              const vid: string = v.video_id || (v.video_url?.match(/BV[\w]+/)?.[0]);
              if (!vid) continue;
              videoInfos.push({
                video_id: vid,
                title: v.title ?? "未知标题",
                desc: v.desc ?? "",
                author: v.author ?? "",
                duration_text: v.duration_text ?? "",
                duration: v.duration,  // 数字时长（秒）
                video_url: v.video_url ?? "",
                view_count: 0,
              });
              // 打开 SSE 流
              if (!activeStreamsRef.current.has(vid)) {
                activeStreamsRef.current.set(vid, _connectSSE(vid, activeStreamsRef));
              }
            }
            if (videoInfos.length > 0) {
              useVideoStore.getState().upsertResults(videoInfos);
            }
          }
          if (ti.state === "result") {
            // 延迟关闭 SSE：给 done 事件留出传输时间，避免竞争
            setTimeout(() => {
              for (const v of batchVideos) {
                const vid: string = v.video_id || (v.video_url?.match(/BV[\w]+/)?.[0]);
                _closeSSE(vid, activeStreamsRef);
              }
            }, 800);
          }
        }
      }
    }
  }, [messages]);

  // 卸载时清理所有 fetch-SSE 连接
  useEffect(() => {
    return () => {
      for (const ctrl of activeStreamsRef.current.values()) {
        ctrl.close();
      }
      activeStreamsRef.current.clear();
    };
  }, []);

  if (messages.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <div className="text-center space-y-2">
          <p className="text-4xl">🎬</p>
          <p className="text-sm">输入指令开始使用 VidAgent</p>
          <p className="text-xs text-muted-foreground/60">
            例如：B站今日热榜前 3 名是什么？
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      {messages.map((m) => (
        <ChatMessage key={m.id} message={m} onVideoClick={onVideoClick} />
      ))}
      {/* 思考/加载指示器：流式进行中，最后一条 assistant 消息尚未完成 */}
      {(status === "submitted" || status === "streaming") && (() => {
        const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
        // 首轮：没有 assistant 消息 → 显示加载
        if (!lastAssistant) return true;
        // 后续轮次：最后一条有 reasoning 但无 content → 仍在思考
        const reasoning = (lastAssistant as any).reasoning;
        const content = lastAssistant.content;
        return (reasoning && !content);
      })() && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm animate-fade-in">
          <Loader2 className="w-4 h-4 animate-spin" />
          思考中…
        </div>
      )}
    </div>
  );
}
