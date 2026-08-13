"use client";

import { memo, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from "react";

/** 从 video_url 提取平台原生 video_id（支持 B站/抖音/YouTube/小红书/快手等）。
 *  必须与后端 _video_task_map 的键一致（后端用 platform.extract_video_id 的纯 ID），
 *  否则 by-video SSE 会 404、卡片进度永远停 0%。 */
function extractVideoId(videoUrl: string): string | null {
  // B站: BVxxx
  const bv = videoUrl.match(/BV[\w]+/);
  if (bv) return bv[0];
  // 抖音: /video/数字ID
  const dy = videoUrl.match(/douyin\.com\/video\/(\d+)/);
  if (dy) return dy[1];
  // YouTube: v=xxx
  const yt = videoUrl.match(/[?&]v=([\w-]{11})/);
  if (yt) return yt[1];
  // 小红书: /explore/xxx 或 /discovery/item/xxx
  const xhs = videoUrl.match(/xiaohongshu\.com\/(?:explore|discovery\/item)\/([\w-]+)/);
  if (xhs) return xhs[1];
  // 快手: /short-video/xxx
  const ks = videoUrl.match(/kuaishou\.com\/short-video\/(\w+)/);
  if (ks) return ks[1];
  return null;
}
import { type Message } from "@ai-sdk/react";
import { cn } from "@/lib/utils";
import { useLayoutStore, useVideoStore, type VideoInfo } from "@/lib/stores";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";
import { streamSummaryByVideo, type SSEController } from "@/lib/api";
import {
  CheckCircle,
  ChevronRight,
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
  batch_summarize_videos: { icon: <FileText className="w-3.5 h-3.5" />, label: "总结内容" },
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
  const selectedVideoId = useLayoutStore((s) => s.selectedVideoId);
  // 从 VideoStore 补全元数据（搜索工具预填的数据比 batch args 更完整）
  const stored = useVideoStore((s) => s.videos[videoId]);

  const isSelected = selectedVideoId === videoId;
  const displayTitle = title !== "未知标题" ? title : (stored?.title ?? title);
  const displayAuthor = author || stored?.author;
  const displayDuration = duration || stored?.duration_text;
  const displaySummary = summary || stored?.summary;
  const displayDesc = stored?.desc;

  // 状态指示器
  const taskStatus = stored?.task_status;
  const downloadProgress = stored?.download_progress ?? 0;
  const isDone = taskStatus === "done";
  const isSummarizing = taskStatus === "extracting" || taskStatus === "summarizing" || taskStatus === "analyzing" || taskStatus === "summary" || taskStatus === "asr" || taskStatus === "thinking" || taskStatus === "chunking" || taskStatus === "merging";
  const isDownloading = taskStatus === "downloading";

  // 构建背景样式
  let statusBgClass = "";
  let isBgOverride = false;
  if (isDone) {
    statusBgClass = "bg-blue-400/15 border-blue-400/20";
    isBgOverride = true;
  } else if (isSummarizing) {
    statusBgClass = "card-status-summarizing";
  }

  // 下载进度：@property --download-pct 支持 CSS transition 平滑过渡渐变百分比
  const progressStyle: React.CSSProperties =
    isDownloading
      ? {
          ["--download-pct" as string]: `${downloadProgress}%`,
          background: `linear-gradient(to right, hsl(217 91% 60% / 0.15) var(--download-pct), transparent var(--download-pct))`,
          transition: "--download-pct 0.3s ease-out",
        }
      : {};

  return (
    // 浮入动画在外层 wrapper：避免与 shimmer 的 animation 属性互相覆盖
    <div className="animate-float-in">
      <button
        onClick={() => selectVideo(videoId)}
        className={cn(
          "w-full text-left rounded-xl border border-border p-4",
          "hover:shadow-md hover:border-primary/20 transition-all duration-200",
          "active:scale-[0.99] cursor-pointer",
          // 默认背景：下载/完成态用自己的背景替代
          !isDownloading && !isBgOverride && "bg-card",
          isSelected && "shadow-md border-primary/20",
          statusBgClass
        )}
        style={progressStyle}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h4 className="text-sm font-medium truncate">{displayTitle}</h4>
            <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
              {displayAuthor && <span>@{displayAuthor}</span>}
              {displayDuration && <span>{displayDuration}</span>}
            </div>
            {displayDesc && (
              <p className="text-xs text-muted-foreground mt-2 line-clamp-2">
                {displayDesc}
              </p>
            )}
          </div>
          <div className="shrink-0 w-24 h-16 rounded-lg bg-muted flex items-center justify-center">
            <span className="text-2xl">🎬</span>
          </div>
        </div>
      </button>
    </div>
  );
});

// ---------------------------------------------------------------------------
// ChatMessage — 单条消息渲染（memo：相同 message 引用时跳过渲染）
// ---------------------------------------------------------------------------

const ChatMessage = memo(function ChatMessage({
  message,
  onVideoClick,
  isStreaming,
}: {
  message: Message;
  onVideoClick: (id: string) => void;
  isStreaming?: boolean;
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
          <AssistantContent
            message={message}
            onVideoClick={onVideoClick}
            isStreaming={isStreaming}
          />
        )}
      </div>
    </div>
  );
}, (prev, next) => prev.message === next.message && prev.isStreaming === next.isStreaming);

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
        const vid = v.video_id || extractVideoId(v.video_url ?? "");
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
          let vid = r.video_id;
          // 按 video_url 二次匹配：Agent 可能未透传 video_id，导致后端 ID 与前端不同
          if (!useVideoStore.getState().videos[vid] && r.video_url) {
            const store = useVideoStore.getState();
            const match = Object.values(store.videos).find(
              (v) => v.video_url === r.video_url,
            );
            if (match) {
              vid = match.video_id;
            }
          }
          // 确保 VideoStore 有该条目
          if (!useVideoStore.getState().videos[vid]) {
            useVideoStore.getState().upsertResults([{
              video_id: vid,
              title: r.title ?? vid,
              desc: "",
              author: "",
              duration_text: "",
              video_url: r.video_url ?? "",
              view_count: 0,
            }]);
          }
          if (r.status === "done" && r.summary) {
            useVideoStore.getState().setSummary(vid, r.summary);
          }
          if (r.local_path) {
            useVideoStore.getState().setLocalPath(vid, r.local_path);
          }
          useVideoStore.getState().updateProgress(vid, {
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
// ThinkingSection — 可折叠思考过程（grid-template-rows 过渡动画）
//
// 替代原生 <details>（display:none 无法过渡）。
// 处理中（inProgress）强制展开，完成后可手动折叠/展开，均带平滑动画。
// ---------------------------------------------------------------------------

function ThinkingSection({
  inProgress,
  lengthText,
  children,
}: {
  inProgress: boolean;
  lengthText: string;
  children: ReactNode;
}) {
  const [manualOpen, setManualOpen] = useState(false);
  const [collapseAnim, setCollapseAnim] = useState(false);
  const isOpen = inProgress || manualOpen;
  // 尾部视图：处理中限高显示最新内容；收起动画期间保持，避免内容突变
  const showTail = inProgress || collapseAnim;

  // 处理结束后自动收起：收起动画完成前保持尾部视图，之后切换为全文（不可见）
  useEffect(() => {
    if (!inProgress && !manualOpen) {
      setCollapseAnim(true);
      const t = setTimeout(() => setCollapseAnim(false), 250);
      return () => clearTimeout(t);
    }
    setCollapseAnim(false);
  }, [inProgress, manualOpen]);

  return (
    <div>
      <button
        onClick={() => setManualOpen((v) => !v)}
        className="flex items-center gap-1 text-xs text-muted-foreground/60 cursor-pointer hover:text-muted-foreground transition-colors select-none"
      >
        <ChevronRight
          className={cn(
            "w-3 h-3 shrink-0 transition-transform duration-200",
            isOpen && "rotate-90"
          )}
        />
        {inProgress && (
          <Loader2 className="w-3 h-3 animate-spin text-blue-500 shrink-0" />
        )}
        <span>思考过程{lengthText}</span>
      </button>
      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-200 ease-out",
          isOpen ? "grid-rows-[1fr]" : "grid-rows-[0fr]"
        )}
      >
        <div className="overflow-hidden min-h-0">
          <div className="mt-2 border-l-2 border-muted pl-3">
            <div
              className={cn(showTail && "max-h-48 overflow-hidden flex items-end")}
            >
              {children}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AssistantContent — 渲染 AI 回复（文本 + tool badges + video cards）
// ---------------------------------------------------------------------------

function AssistantContent({
  message,
  onVideoClick,
  isStreaming,
}: {
  message: Message;
  onVideoClick: (id: string) => void;
  isStreaming?: boolean;
}) {
  const toolInvocations = (message as any).toolInvocations ?? [];
  const reasoning = (message as any).reasoning as string | undefined;

  // 消息是否仍在处理中：流式未结束 或 有工具正在执行
  const hasActiveTools = toolInvocations.some(
    (ti: any) => ti.state === "call" || ti.state === "partial-call",
  );
  const inProgress = isStreaming || hasActiveTools;

  // 从 batch_summarize_videos 工具调用中提取视频卡片
  // （搜索工具不显示卡片——未总结的视频卡片没有意义）
  const videoCards = toolInvocations
    .filter((ti: any) => ti.toolName === "batch_summarize_videos")
    .flatMap((ti: any) => {
      const batchVideos: any[] = ti.args?.videos ?? [];
      // 查找已完成的结果，用于显示 summary
      const results: any[] = ti.result?.results ?? [];
      const resultByUrl = new Map<string, any>();
      for (const r of results) {
        if (r.video_url) resultByUrl.set(r.video_url, r);
      }
      return batchVideos.map((v: any, i: number) => {
        const vid = v.video_id || extractVideoId(v.video_url ?? "") || `batch-${i}`;
        const result = resultByUrl.get(v.video_url);
        return (
          <VideoCard
            key={vid}
            videoId={vid}
            title={v.title ?? "未知标题"}
            author={v.author}
            duration={v.duration_text}
            summary={result?.summary}
          />
        );
      });
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

      {/* 推理过程（可折叠，模型思考内容）— 处理期间保持展开，多次思考连续累积显示 */}
      {reasoning && (
        <ThinkingSection
          inProgress={inProgress}
          lengthText={reasoning.length > 0 ? `（${reasoning.length} 字）` : ""}
        >
          <p className="text-xs text-muted-foreground/50 whitespace-pre-wrap leading-relaxed">
            {reasoning}
          </p>
        </ThinkingSection>
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
      // 分块进度（长视频分段总结）
      if (data.chunks) {
        store.setChunks(videoId, data.chunks);
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
              const vid: string = v.video_id || extractVideoId(v.video_url ?? "");
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
                task_status: "downloading",
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
                const vid: string = v.video_id || extractVideoId(v.video_url ?? "");
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
          <p className="text-sm flex items-center justify-center gap-1">
            输入指令开始使用
            <img
              src="/logos/VidAgentLogo.png"
              alt="VidAgent"
              className="h-4 w-auto"
            />
          </p>
        </div>
      </div>
    );
  }

  return (
    <div>
      {messages.map((m, idx) => (
        <ChatMessage
          key={m.id}
          message={m}
          onVideoClick={onVideoClick}
          isStreaming={
            idx === messages.length - 1 &&
            (status === "streaming" || status === "submitted")
          }
        />
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
