"use client";

import { type Message } from "@ai-sdk/react";
import { cn } from "@/lib/utils";
import { useLayoutStore } from "@/lib/stores";
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

const TOOL_META: Record<string, { icon: React.ReactNode; label: string }> = {
  get_hot_videos: { icon: <Flame className="w-3.5 h-3.5" />, label: "热门" },
  search_videos: { icon: <Search className="w-3.5 h-3.5" />, label: "搜索" },
  get_creator_videos: { icon: <User className="w-3.5 h-3.5" />, label: "创作者" },
  download_video: { icon: <Download className="w-3.5 h-3.5" />, label: "下载" },
  extract_and_summarize: { icon: <FileText className="w-3.5 h-3.5" />, label: "总结" },
};

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

export function VideoCard({
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
}

// ---------------------------------------------------------------------------
// ChatMessage — 单条消息渲染
// ---------------------------------------------------------------------------

function ChatMessage({
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
  // 工具调用徽章
  const toolInvocations = (message as any).toolInvocations ?? [];

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

      {/* 文本内容（Markdown 渲染） */}
      {message.content && (
        <div className="prose prose-sm dark:prose-invert max-w-none text-sm leading-relaxed">
          {renderContent(message.content)}
        </div>
      )}

      {/* 视频卡片（从工具结果中提取） */}
      {toolInvocations
        .filter((ti: any) => ti.state === "result" && ti.toolName === "get_hot_videos")
        .flatMap((ti: any) => {
          const results = ti.result?.results ?? [];
          return results.slice(0, 3).map((v: any, i: number) => (
            <VideoCard
              key={v.video_id ?? i}
              videoId={v.video_id ?? String(i)}
              title={v.title ?? "未知标题"}
              author={v.author}
              duration={v.duration_text}
              summary={v.desc}
            />
          ));
        })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 简易 Markdown 渲染
// ---------------------------------------------------------------------------

function renderContent(text: string): React.ReactNode {
  const lines = text.split("\n");
  return lines.map((line, i) => {
    // 标题
    if (line.startsWith("### ")) {
      return (
        <h4 key={i} className="text-sm font-semibold mt-3 mb-1">
          {line.slice(4)}
        </h4>
      );
    }
    if (line.startsWith("## ")) {
      return (
        <h3 key={i} className="text-base font-semibold mt-4 mb-2">
          {line.slice(3)}
        </h3>
      );
    }
    // 加粗
    const bolded = line.replace(
      /\*\*(.+?)\*\*/g,
      "<strong>$1</strong>"
    );
    if (!bolded.trim()) {
      return <div key={i} className="h-2" />;
    }
    return (
      <p
        key={i}
        className="min-h-[1.4em]"
        dangerouslySetInnerHTML={{ __html: bolded }}
      />
    );
  });
}

// ---------------------------------------------------------------------------
// ChatView — 消息列表
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
      {status === "submitted" && !messages.some((m) => m.role === "assistant") && (
        <div className="flex items-center gap-2 text-muted-foreground text-sm animate-fade-in">
          <Loader2 className="w-4 h-4 animate-spin" />
          思考中…
        </div>
      )}
    </div>
  );
}
