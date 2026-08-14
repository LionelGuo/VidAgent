// ⚠️ GENERATED FILE — 勿手改。
// 来源：scripts/gen-sse-types.py（解析 server/models.py 的 TaskStatus、
// src/vidagent/tools/summarize/progress.py 的 ProgressStage 与分段状态字面量）。
// 重新生成：python scripts/gen-sse-types.py
// 一致性检查：python scripts/gen-sse-types.py --check
// 本文件是总结进度 SSE（Channel B）的前后端共享词汇表；wire 字节等价契约
// 由后端测试钉死（tests/test_task_models.py）。

/** 总结任务阶段（ProgressStage）。"" 为空闲哨兵：未开始/已复位，可出现在事件流中。 */
export const SUMMARY_STAGES = [
  "downloading",
  "downloaded",
  "extracting",
  "summarizing",
  "thinking",
  "summary",
  "chunking",
  "merging",
] as const;

/** wire 阶段：枚举成员 ∪ 空闲哨兵 ""（与 Python 侧 ProgressStage | Literal[""] 对称）。 */
export type SummaryStage = "" | (typeof SUMMARY_STAGES)[number];

/** 总结任务终态（TaskStatus）。 */
export const TASK_STATUSES = [
  "processing",
  "done",
  "error",
] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

/** 长视频分段进度条目（progress 事件 chunks 数组元素）。
 *  status 字面量自动提取自 summarize/multimodal.py 的 chunk["status"] 赋值点。 */
export interface SummaryChunk {
  index: number;
  total: number;
  time_start: number;
  time_end: number;
  status: "waiting" | "done";
  text: string;
}

/**
 * 总结进度 SSE（Channel B）事件联合。
 * progress 载荷有四种互斥形态（stage 变化 / downloaded 瞬态 / chunks 变化 /
 * 流式文本），flat 可选键如实反映发射端事实；done / error 为终态。
 */
export type SummarySSEProgress = {
  type: "progress";
  stage?: SummaryStage;
  download_pct?: number;
  local_path?: string;
  chunks?: SummaryChunk[];
  message?: string;
};

export type SummarySSEDone = {
  type: "done";
  result: string;
  local_path: string;
};

export type SummarySSEError = {
  type: "error";
  message: string;
};

export type SummarySSEEvent = SummarySSEProgress | SummarySSEDone | SummarySSEError;
