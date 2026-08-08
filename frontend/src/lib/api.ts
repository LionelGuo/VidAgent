/** API 客户端 */

export const apiBaseUrl =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

/** 从后端获取工具定义 */
export async function fetchToolDefinitions() {
  const res = await fetch(`${apiBaseUrl}/api/tools/definitions`);
  if (!res.ok) throw new Error(`Failed to fetch tool definitions: ${res.status}`);
  return res.json();
}

/** 下载视频 */
export async function downloadVideo(videoUrl: string, fileName: string) {
  const res = await fetch(`${apiBaseUrl}/api/tools/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_url: videoUrl, file_name: fileName }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "下载失败");
  }
  return res.json();
}

/** 启动总结任务 */
export async function startSummarize(localPath: string, metadata?: Record<string, unknown>) {
  const res = await fetch(`${apiBaseUrl}/api/tools/summarize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ local_path: localPath, metadata }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "总结启动失败");
  }
  return res.json() as Promise<{ task_id: string; stream_url: string }>;
}

/** 创建 SSE 连接到总结进度流（按 task_id） */
export function createSummarizeStream(taskId: string): EventSource {
  return new EventSource(
    `${apiBaseUrl}/api/tools/summarize/${taskId}/stream`
  );
}

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
          // 任务不存在（正常的延迟竞态）
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
  onProgress: (text: string) => void,
  onDone: (result: string) => void,
  onError?: (err: Error) => void,
): SSEController {
  return fetchSSE(
    `${apiBaseUrl}/api/tools/summarize/by-video/${videoId}/stream`,
    (data) => {
      if (data.type === "progress" && data.stage === "summary") {
        onProgress(data.message);
      } else if (data.type === "done") {
        onDone(data.result || "");
      } else if (data.type === "error") {
        onError?.(new Error(data.message || "总结失败"));
      }
    },
    onError,
  );
}
