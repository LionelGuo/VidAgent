"use client";

import { useChat } from "@ai-sdk/react";
import { ChatView } from "@/components/chat/ChatView";
import { DetailPanel } from "@/components/detail/DetailPanel";
import { useLayoutStore } from "@/lib/stores";
import { cn } from "@/lib/utils";

export default function Home() {
  const selectedVideoId = useLayoutStore((s) => s.selectedVideoId);

  // Phase 2: 纯文本流式验证，通过 Next.js API Route → FastAPI SSE Relay → vLLM
  const { messages, input, handleInputChange, handleSubmit, status } = useChat({
    api: "/api/chat",
    onError: (err) => console.error("Chat error:", err),
  });

  return (
    <div className="flex h-screen bg-background">
      {/* 左侧聊天面板 */}
      <div
        className={cn(
          "flex flex-col min-w-0 transition-all duration-300",
          selectedVideoId
            ? "w-[45%] border-r border-border"
            : "w-full max-w-3xl mx-auto"
        )}
      >
        {/* Header */}
        <header className="shrink-0 px-6 py-4 border-b border-border">
          <h1 className="text-lg font-semibold">🎬 VidAgent</h1>
          <p className="text-sm text-muted-foreground">
            视频采集与多模态总结助手
          </p>
        </header>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-6">
          <ChatView
            messages={messages}
            status={status}
            onVideoClick={(id) => useLayoutStore.getState().selectVideo(id)}
          />
        </div>

        {/* Input */}
        <form
          onSubmit={handleSubmit}
          className="shrink-0 border-t border-border p-4"
        >
          <div className="flex gap-2">
            <input
              value={input}
              onChange={handleInputChange}
              placeholder="输入指令，如：抓 B站 今日热榜前 3 并总结…"
              disabled={status === "streaming" || status === "submitted"}
              className="flex-1 rounded-lg border border-input bg-background px-4 py-3 text-sm
                         placeholder:text-muted-foreground focus:outline-none focus:ring-2
                         focus:ring-ring disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={
                !input.trim() || status === "streaming" || status === "submitted"
              }
              className="rounded-lg bg-primary px-6 py-3 text-sm font-medium
                         text-primary-foreground hover:opacity-90 disabled:opacity-50
                         transition-all active:scale-95"
            >
              {status === "streaming" || status === "submitted" ? "…" : "发送"}
            </button>
          </div>
        </form>
      </div>

      {/* 右侧详情面板 */}
      {selectedVideoId && (
        <div className="w-[55%] animate-fade-in">
          <DetailPanel
            videoId={selectedVideoId}
            onClose={() => useLayoutStore.getState().selectVideo(null)}
          />
        </div>
      )}
    </div>
  );
}
