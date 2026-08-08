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

/** 创建 SSE 连接到总结进度流（按 video_id，浏览器端使用） */
export function createSummaryStreamByVideo(videoId: string): EventSource {
  return new EventSource(
    `${apiBaseUrl}/api/tools/summarize/by-video/${videoId}/stream`
  );
}
