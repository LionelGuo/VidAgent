#!/usr/bin/env python3
"""生成 frontend/src/lib/sse-events.ts（#1 SSE 共享 schema 的 codegen）。

解析后端枚举的字符串字面量（server/models.py 的 TaskStatus 与
src/vidagent/tools/summarize/progress.py 的 ProgressStage），与事件 shape 模板
拼装为 TypeScript 类型文件。枚举是词汇表的单一来源，事件 shape 模板在此
脚本内，一次重新生成即整体更新，防止前后端词汇漂移（stores.ts 曾残留
后端已不发出的 asr/analyzing）。

--check 模式对比生成结果与磁盘文件，不一致退出 1（CI 与本地 pytest 使用）。
纯 stdlib（ast），无第三方依赖。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "frontend" / "src" / "lib" / "sse-events.ts"

ENUM_SOURCES: list[tuple[Path, str]] = [
    (REPO_ROOT / "server" / "models.py", "TaskStatus"),
    (REPO_ROOT / "src" / "vidagent" / "tools" / "summarize" / "progress.py", "ProgressStage"),
]

CHUNK_STATUS_SOURCE = REPO_ROOT / "src" / "vidagent" / "tools" / "summarize" / "multimodal.py"

HEADER = """\
// ⚠️ GENERATED FILE — 勿手改。
// 来源：scripts/gen-sse-types.py（解析 server/models.py 的 TaskStatus、
// src/vidagent/tools/summarize/progress.py 的 ProgressStage 与分段状态字面量）。
// 重新生成：python scripts/gen-sse-types.py
// 一致性检查：python scripts/gen-sse-types.py --check
// 本文件是总结进度 SSE（Channel B）的前后端共享词汇表；wire 字节等价契约
// 由后端测试钉死（tests/test_task_models.py）。
"""


def extract_str_enum_values(path: Path, class_name: str) -> list[str]:
    """返回 class_name 的枚举成员字符串值（类体声明序）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            values: list[str] = []
            for stmt in node.body:
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    continue  # 跳过 docstring / 方法 / 无关语句
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = stmt.value
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        values.append(value.value)
            if not values:
                raise SystemExit(f"{path}:{class_name}: 未提取到字符串枚举成员")
            return values
    raise SystemExit(f"{path}: 未找到类 {class_name}")


def extract_chunk_status_values() -> list[str]:
    """提取 summarize/multimodal.py 的分段状态字面量（两种写入形态，文件出现序）。

    chunk["status"] = "…" 赋值与 "status": "…" 字典字面量。用正则而非 AST：
    这是守卫而非解析器——宁多收（--check 提醒，假阳性安全方向），不漏收。
    """
    text = CHUNK_STATUS_SOURCE.read_text(encoding="utf-8")
    pattern = r'\["status"\]\s*=\s*"([^"]*)"|"status":\s*"([^"]*)"'
    values: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(pattern, text):
        value = match.group(1) or match.group(2)
        if value not in seen:
            seen.add(value)
            values.append(value)
    if not values:
        raise SystemExit(f"{CHUNK_STATUS_SOURCE}: 未提取到分段状态字面量")
    return values


def render(stages: list[str], statuses: list[str], chunk_statuses: list[str]) -> str:
    """组装生成文件全文（枚举值来自 AST，shape 来自模板）。"""
    stage_lines = "\n".join(f"  {json.dumps(v)}," for v in stages)
    status_lines = "\n".join(f"  {json.dumps(v)}," for v in statuses)
    chunk_status_union = " | ".join(json.dumps(v) for v in chunk_statuses)
    return f"""{HEADER}
/** 总结任务阶段（ProgressStage）。"" 为空闲哨兵：未开始/已复位，可出现在事件流中。 */
export const SUMMARY_STAGES = [
{stage_lines}
] as const;

/** wire 阶段：枚举成员 ∪ 空闲哨兵 ""（与 Python 侧 ProgressStage | Literal[""] 对称）。 */
export type SummaryStage = "" | (typeof SUMMARY_STAGES)[number];

/** 总结任务终态（TaskStatus）。 */
export const TASK_STATUSES = [
{status_lines}
] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

/** 长视频分段进度条目（progress 事件 chunks 数组元素）。
 *  status 字面量自动提取自 summarize/multimodal.py 的 chunk["status"] 赋值点。 */
export interface SummaryChunk {{
  index: number;
  total: number;
  time_start: number;
  time_end: number;
  status: {chunk_status_union};
  text: string;
}}

/**
 * 总结进度 SSE（Channel B）事件联合。
 * progress 载荷有四种互斥形态（stage 变化 / downloaded 瞬态 / chunks 变化 /
 * 流式文本），flat 可选键如实反映发射端事实；done / error 为终态。
 */
export type SummarySSEProgress = {{
  type: "progress";
  stage?: SummaryStage;
  download_pct?: number;
  local_path?: string;
  chunks?: SummaryChunk[];
  message?: string;
}};

export type SummarySSEDone = {{
  type: "done";
  result: string;
  local_path: string;
}};

export type SummarySSEError = {{
  type: "error";
  message: string;
}};

export type SummarySSEEvent = SummarySSEProgress | SummarySSEDone | SummarySSEError;
"""


def generate() -> str:
    """按类名取词（与 ENUM_SOURCES 顺序无关），防止两枚举值互换。"""
    values = {name: extract_str_enum_values(path, name) for path, name in ENUM_SOURCES}
    return render(values["ProgressStage"], values["TaskStatus"], extract_chunk_status_values())


def check() -> int:
    expected = generate()
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == expected:
        print(f"up to date: {OUTPUT}")
        return 0
    print(f"stale: {OUTPUT}（与后端枚举不一致，请运行 python scripts/gen-sse-types.py 重新生成）")
    return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        return check()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generate(), encoding="utf-8")
    print(f"generated: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
