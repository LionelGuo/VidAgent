"use client";

import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// MarkdownRenderer — 共享 Markdown 渲染组件
//
// 封装 react-markdown + remark-gfm（GFM 表格/删除线/任务列表）。
// 使用 Tailwind prose 排版，统一聊天区和详情面板的渲染效果。
// React.memo：避免相同 children 时重复解析 Markdown（流式场景关键优化）。
// ---------------------------------------------------------------------------

interface MarkdownRendererProps {
  children: string;
  className?: string;
}

export const MarkdownRenderer = memo(function MarkdownRenderer({
  children,
  className,
}: MarkdownRendererProps) {
  return (
    <div
      className={cn(
        "prose prose-sm dark:prose-invert max-w-none",
        // 覆盖：让 prose 内的文本颜色跟随父级
        "prose-p:text-foreground prose-p:text-sm prose-p:leading-relaxed prose-p:my-1.5",
        "prose-li:text-foreground prose-li:text-sm prose-li:leading-relaxed",
        "prose-strong:text-foreground prose-strong:font-semibold",
        "prose-a:text-primary prose-a:underline prose-a:underline-offset-2",
        "prose-code:text-foreground/90 prose-code:bg-muted prose-code:rounded prose-code:px-1 prose-code:py-0.5 prose-code:text-xs",
        "prose-pre:bg-muted prose-pre:border prose-pre:border-border prose-pre:rounded-lg",
        "prose-h1:text-lg prose-h1:font-semibold prose-h1:text-foreground prose-h1:mt-4 prose-h1:mb-2",
        "prose-h2:text-base prose-h2:font-semibold prose-h2:text-foreground prose-h2:mt-4 prose-h2:mb-2",
        "prose-h3:text-sm prose-h3:font-semibold prose-h3:text-foreground prose-h3:mt-3 prose-h3:mb-1.5",
        "prose-h4:text-sm prose-h4:font-medium prose-h4:text-foreground prose-h4:mt-2 prose-h4:mb-1",
        "prose-headings:text-foreground",
        "prose-th:text-sm prose-th:font-semibold prose-th:p-2 prose-th:border prose-th:border-border prose-th:bg-muted",
        "prose-td:text-sm prose-td:p-2 prose-td:border prose-td:border-border",
        "prose-table:border prose-table:border-border prose-table:rounded-lg",
        "prose-blockquote:border-l-2 prose-blockquote:border-primary/30 prose-blockquote:pl-4 prose-blockquote:text-muted-foreground prose-blockquote:not-italic",
        "prose-hr:border-border",
        "prose-ul:my-1.5 prose-ol:my-1.5",
        "prose-img:rounded-lg",
        className
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {children}
      </ReactMarkdown>
    </div>
  );
});
