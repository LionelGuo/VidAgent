/** API 客户端 */

import type {
  SummarySSEDone,
  SummarySSEEvent,
  SummarySSEProgress,
} from "./sse-events";

export const apiBaseUrl =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

// ---------------------------------------------------------------------------
// fetch-based SSE（替代 EventSource，无自动重连）
// ---------------------------------------------------------------------------

export interface SSEController {
  /** 关闭连接（不会重连） */
  close: () => void;
}

/**
 * 用 fetch + ReadableStream 消费 SSE 流。
 * 相比 EventSource 的优势：
 * - 不会自动重连（根除幽灵连接）
 * - 完全控制连接生命周期
 * - 支持 AbortController 精确清理
 */
export function fetchSSE(
  url: string,
  onEvent: (data: any) => void,
  onError?: (err: Error) => void,
): SSEController {
  const abort = new AbortController();
  let closed = false;

  (async () => {
    try {
      const res = await fetch(url, {
        signal: abort.signal,
        headers: { Accept: "text/event-stream" },
      });

      if (!res.ok) {
        if (res.status === 404 && !closed) {
          // mapping 尚未注册（AI SDK 时序：SSE 可能在 POST 之前到达），重试等待
          for (let attempt = 0; attempt < 15 && !closed; attempt++) {
            await new Promise((r) => setTimeout(r, 200));
            const retryRes = await fetch(url, {
              signal: abort.signal,
              headers: { Accept: "text/event-stream" },
            }).catch(() => null);
            if (retryRes && retryRes.ok) {
              // 重试成功，继续用新的 response 处理
              const retryReader = retryRes.body?.getReader();
              if (!retryReader) break;
              // 解析 SSE（内联处理）
              const processRetry = async () => {
                const dec = new TextDecoder();
                let buf = "";
                try {
                  while (!closed) {
                    const { done: d, value: v } = await retryReader.read();
                    if (d) break;
                    buf += dec.decode(v, { stream: true });
                    const ls = buf.split("\n");
                    buf = ls.pop() || "";
                    for (const l of ls) {
                      if (!l.startsWith("data: ") || l === "data: [DONE]") continue;
                      try { if (!closed) onEvent(JSON.parse(l.slice(6))); } catch {}
                    }
                  }
                } catch {}
              };
              processRetry();
              return;
            }
          }
          // 重试耗尽，通知错误
          if (!closed) onError?.(new Error("总结任务超时未就绪"));
          return;
        }
        throw new Error(`SSE HTTP ${res.status}`);
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error("无响应体");

      const decoder = new TextDecoder();
      let buffer = "";

      while (!closed) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          if (line === "data: [DONE]") continue;

          try {
            const data = JSON.parse(line.slice(6));
            if (!closed) onEvent(data);
          } catch {
            // 跳过非 JSON 行
          }
        }
      }
    } catch (err: any) {
      if (err.name === "AbortError") return; // 主动关闭，正常
      if (!closed) onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  })();

  return {
    close: () => {
      closed = true;
      abort.abort();
    },
  };
}

/**
 * 按 video_id 连接总结进度 SSE（fetch 版，无自动重连）。
 * 返回 SSEController，调用方负责 close。
 */
export function streamSummaryByVideo(
  videoId: string,
  onProgress: (data: SummarySSEProgress) => void,
  onDone: (
    result: string,
    extra: Pick<SummarySSEDone, "local_path" | "chapters">,
  ) => void,
  onError?: (err: Error) => void,
): SSEController {
  return fetchSSE(
    `${apiBaseUrl}/api/tools/summarize/by-video/${videoId}/stream`,
    (data: SummarySSEEvent) => {
      if (data.type === "progress") {
        // 传递完整 data 对象（含 stage / download_pct / message）
        onProgress(data);
      } else if (data.type === "done") {
        onDone(data.result || "", { local_path: data.local_path, chapters: data.chapters });
      } else if (data.type === "error") {
        onError?.(new Error(data.message || "总结失败"));
      }
    },
    onError,
  );
}
