"use client";

import { useChat } from "@ai-sdk/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChatView } from "@/components/chat/ChatView";
import { DetailPanel } from "@/components/detail/DetailPanel";
import { useLayoutStore } from "@/lib/stores";
import { cn } from "@/lib/utils";

export default function Home() {
  const selectedVideoId = useLayoutStore((s) => s.selectedVideoId);
  const [closing, setClosing] = useState(false);
  const [entered, setEntered] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const overlayRef = useRef<HTMLDivElement>(null);

  const { messages, input, handleInputChange, handleSubmit, status } = useChat({
    api: "/api/chat",
    onError: (err) => console.error("Chat error:", err),
  });

  // ── 打开：mount 帧 → 下一帧 entered=true，CSS transition 驱动卡片的滑入 ──
  useEffect(() => {
    if (selectedVideoId) {
      setEntered(false);
      const raf = requestAnimationFrame(() => setEntered(true));
      return () => cancelAnimationFrame(raf);
    }
  }, [selectedVideoId]);

  // ── 关闭：切到 off-screen → CSS transition 滑出 → transitionend 后真正 unmount ──
  const handleClose = useCallback(() => {
    // 全屏状态下关闭：先退出全屏，让 transition 从全屏位置滑出
    setExpanded(false);
    setClosing(true);
  }, []);

  const handleTransitionEnd = useCallback((e: React.TransitionEvent) => {
    // 只等 opacity 淡出完成才卸载（transitionend 对每个属性各触发一次）
    if (closing && e.propertyName === "opacity") {
      useLayoutStore.getState().selectVideo(null);
      setClosing(false);
      setEntered(false);
      setExpanded(false);
    }
  }, [closing]);

  const showOverlay = selectedVideoId !== null;

  // ── 全屏切换：expanded 改变 → CSS transition 平滑过渡四边 ──
  const toggleFullscreen = useCallback(() => {
    setExpanded((p) => !p);
  }, []);

  return (
    <div className="flex flex-col h-screen bg-background">
      {/* ── 上方区域 ── */}
      <div className="flex-1 flex min-h-0">
        {/* 左侧聊天面板 — width + marginLeft 同步过渡 */}
        <div
          className="flex flex-col min-w-0"
          style={{
            width: entered && !closing ? "41.5%" : "48rem",
            marginLeft: entered && !closing
              ? "1.5%"
              : "calc((100% - 48rem) / 2)",
            transition:
              "width 0.5s cubic-bezier(0.16, 1, 0.3, 1), margin-left 0.5s cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <header className="shrink-0 px-6 pt-7 pb-4 border-b border-border">
            <div className="flex items-center gap-2.5">
              <img
                src="/logos/VALogo.png"
                alt="VidAgent"
                className="h-6 w-auto"
              />
              <h1 className="text-lg font-semibold">VidAgent</h1>
            </div>
          </header>

          <div
            className={cn(
              "flex-1 overflow-y-auto pl-8 pr-3 py-6",
              // 详情卡片展开时隐藏对话区滚动条（避免两条滚动条并存）
              showOverlay && "no-scrollbar"
            )}
          >
            <ChatView
              messages={messages}
              status={status}
              onVideoClick={(id) => useLayoutStore.getState().selectVideo(id)}
            />
          </div>
        </div>

        {/* ── 详情卡片：始终 fixed 定位，CSS transition 驱动所有动画 ── */}
        {showOverlay && (
          <div
            ref={overlayRef}
            onTransitionEnd={handleTransitionEnd}
            className={cn(
              "fixed z-40",
              "duration-1000 ease-out",
              // 全屏模式：四边距视口 1rem
              expanded && "inset-4 opacity-100",
              // 侧边栏 + 关闭：固定上下 + 右边距，left 动态变化
              !expanded && "top-4 bottom-24 right-4",
              // 侧边栏可见：left 对齐对话区右侧 + 1% 间隙
              entered && !closing && !expanded && "left-[44%] opacity-100",
              // 进入前 / 关闭滑出：left 跟踪对话区右边界（~78%），保持间隙恒定
              (!entered || closing) && !expanded && "left-[78%] opacity-0",
            )}
            style={{
              // 打开时卡片延迟 75ms 跟随对话区，关闭时同步进行
              // cubic-bezier(0.16, 1, 0.3, 1) = easeOutExpo，更强的快起慢收
              transition: closing
                ? "left 0.5s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.5s cubic-bezier(0.16, 1, 0.3, 1), bottom 0.5s cubic-bezier(0.16, 1, 0.3, 1)"
                : "left 0.5s cubic-bezier(0.16, 1, 0.3, 1) 75ms, opacity 0.5s cubic-bezier(0.16, 1, 0.3, 1) 75ms, bottom 0.5s cubic-bezier(0.16, 1, 0.3, 1) 75ms",
            }}
          >
            <DetailPanel
              videoId={selectedVideoId!}
              expanded={expanded}
              onToggleFullscreen={toggleFullscreen}
              onClose={handleClose}
            />
          </div>
        )}
      </div>

      {/* ── 底部输入框 ── */}
      <form
        onSubmit={handleSubmit}
        className="shrink-0 border-t border-border p-4 bg-background"
      >
        <div className="flex gap-2 max-w-3xl mx-auto">
          <input
            value={input}
            onChange={handleInputChange}
            placeholder="快速了解各大平台视频内容"
            disabled={status === "streaming" || status === "submitted"}
            className="flex-1 rounded-full border border-input bg-background px-5 py-3 text-sm
                       placeholder:text-muted-foreground focus:outline-none focus:ring-2
                       focus:ring-ring disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={
              !input.trim() || status === "streaming" || status === "submitted"
            }
            className="rounded-full bg-primary px-6 py-3 text-sm font-medium
                       text-primary-foreground hover:opacity-90 disabled:opacity-50
                       transition-all active:scale-95"
          >
            {status === "streaming" || status === "submitted" ? "…" : "发送"}
          </button>
        </div>
      </form>
    </div>
  );
}
