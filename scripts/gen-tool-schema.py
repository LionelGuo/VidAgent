#!/usr/bin/env python3
"""生成 frontend/src/lib/tool-schema.ts（#8 工具 schema 结构化知识的 codegen）。

解析后端单一来源：
- src/vidagent/tools/platforms/__init__.py 的 PLATFORM_MODULES（平台清单）
  与 VIDEO_FIELDS（统一字段清单）
- 各平台类的 supports_hot / supports_search / supports_creator /
  capability_notes 声明（能力矩阵，含使用条件备注）
- server/main.py 的 DEFAULT_PLATFORM / DEFAULT_LIMIT（工具 API 默认值）

生成前端 zod/describe 引用的结构化片段（平台句 / 字段清单文本 / 默认值）。
人工行为指导 prose（「【推荐】」「应引导用户改用搜索」等）不生成——
Q8a 定案：codegen 只覆盖结构化知识，散文提示词人工维护。

--check 模式对比生成结果与磁盘文件，不一致退出 1（CI 与本地 pytest 使用）。
纯 stdlib（ast），无第三方依赖。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "frontend" / "src" / "lib" / "tool-schema.ts"

PLATFORMS_INIT = REPO_ROOT / "src" / "vidagent" / "tools" / "platforms" / "__init__.py"
MAIN_PY = REPO_ROOT / "server" / "main.py"

HEADER = """\
// ⚠️ GENERATED FILE — 勿手改。
// 来源：scripts/gen-tool-schema.py（解析平台能力声明 supports_* / capability_notes、
// platforms/__init__.py 的 PLATFORM_MODULES / VIDEO_FIELDS、server/main.py 的
// DEFAULT_PLATFORM / DEFAULT_LIMIT）。
// 重新生成：python scripts/gen-tool-schema.py
// 一致性检查：python scripts/gen-tool-schema.py --check
// 本文件是工具 schema 结构化知识的单一来源：平台清单 / 能力矩阵 / 字段清单 /
// 默认值。前端手写 describe 引用本文件的生成片段，人工行为指导 prose 不在此处。
"""

_CN_NUMS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _module_assigns(path: Path) -> dict[str, ast.expr]:
    """模块级简单赋值/注解赋值：名字 → 值表达式。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            result[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node.value
    return result


def _const_str(value: ast.expr | None, *, name: str, where: str) -> str:
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
        raise SystemExit(f"{where}: {name} 必须是字符串常量")
    return value.value


def _const_bool(value: ast.expr | None, *, name: str, where: str) -> bool:
    if not (isinstance(value, ast.Constant) and isinstance(value.value, bool)):
        raise SystemExit(f"{where}: {name} 必须是布尔常量")
    return value.value


def extract_platform_modules() -> list[str]:
    value = _module_assigns(PLATFORMS_INIT)["PLATFORM_MODULES"]
    if not isinstance(value, (ast.Tuple, ast.List)):
        raise SystemExit(f"{PLATFORMS_INIT}: PLATFORM_MODULES 必须是元组/列表")
    return [_const_str(elt, name="PLATFORM_MODULES 元素", where=str(PLATFORMS_INIT)) for elt in value.elts]


def extract_video_fields() -> list[str]:
    value = _module_assigns(PLATFORMS_INIT)["VIDEO_FIELDS"]
    if not isinstance(value, (ast.Tuple, ast.List)):
        raise SystemExit(f"{PLATFORMS_INIT}: VIDEO_FIELDS 必须是元组/列表")
    return [_const_str(elt, name="VIDEO_FIELDS 元素", where=str(PLATFORMS_INIT)) for elt in value.elts]


def extract_defaults() -> tuple[str, int]:
    assigns = _module_assigns(MAIN_PY)
    platform = _const_str(assigns["DEFAULT_PLATFORM"], name="DEFAULT_PLATFORM", where=str(MAIN_PY))
    limit = assigns["DEFAULT_LIMIT"]
    if not (isinstance(limit, ast.Constant) and isinstance(limit.value, int)):
        raise SystemExit(f"{MAIN_PY}: DEFAULT_LIMIT 必须是整数常量")
    return platform, limit.value


def _class_assigns(cls: ast.ClassDef) -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            result[stmt.target.id] = stmt.value
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            result[stmt.targets[0].id] = stmt.value
    return result


def _parse_notes(value: ast.expr | None, *, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, ast.Dict):
        raise SystemExit(f"{where}: capability_notes 必须是字典字面量")
    notes: dict[str, str] = {}
    for key, val in zip(value.keys, value.values, strict=True):
        k = _const_str(key, name="capability_notes 键", where=where)
        notes[k] = _const_str(val, name="capability_notes 值", where=where)
    return notes


def extract_capabilities(module_names: list[str]) -> dict[str, dict]:
    """各平台类的能力声明（AST 静态提取，不 import——避免触发 MC 依赖）。"""
    caps: dict[str, dict] = {}
    for module_name in module_names:
        module_file = module_name.split(".")[-1]
        path = PLATFORMS_INIT.parent / f"{module_file}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            assigns = _class_assigns(node)
            if "supports_hot" not in assigns:
                continue  # 非平台类（如错误类型）
            where = f"{path}:{node.name}"
            name = _const_str(assigns.get("name"), name="name", where=where)
            caps[name] = {
                "hot": _const_bool(assigns.get("supports_hot"), name="supports_hot", where=where),
                "search": _const_bool(assigns.get("supports_search"), name="supports_search", where=where),
                "creator": _const_bool(assigns.get("supports_creator"), name="supports_creator", where=where),
                "notes": _parse_notes(assigns.get("capability_notes"), where=where),
            }
    missing = [m for m in module_names if m.split(".")[-1] not in caps]
    if missing:
        raise SystemExit(f"平台模块缺能力声明: {missing}")
    return caps


def _platform_sentence(order: list[str], caps: dict[str, dict], capability: str, label: str) -> str:
    supported = [n for n in order if caps[n][capability]]
    names = " / ".join(supported)
    if capability == "hot":
        unsupported = [n for n in order if not caps[n][capability]]
        if unsupported:
            return f"平台：{names}（{'、'.join(unsupported)} 不支持热榜）"
        return f"平台：{names}"
    suffix = f"{_CN_NUMS[len(supported)]}平台均支持{label}"
    notes = [caps[n]["notes"].get(capability) for n in supported if caps[n]["notes"].get(capability)]
    if notes:
        suffix += "；" + "；".join(notes)
    return f"平台：{names}（{suffix}）"


def render(
    platform_names: list[str],
    caps: dict[str, dict],
    video_fields: list[str],
    default_platform: str,
    default_limit: int,
) -> str:
    """组装生成文件全文（值来自 AST，句式来自模板）。"""
    # ensure_ascii=False：生成文件里的中文保持原样（可读性；TS 字符串字面量
    # 允许 UTF-8，json.dumps 仅转义引号/反斜杠/控制字符）
    platforms_lines = "\n".join(f"  {json.dumps(n, ensure_ascii=False)}," for n in platform_names)
    fields_lines = "\n".join(f"  {json.dumps(f, ensure_ascii=False)}," for f in video_fields)
    cap_lines = "\n".join(
        "  {name}: {{ hot: {hot}, search: {search}, creator: {creator}, notes: {notes} }},".format(
            name=json.dumps(n, ensure_ascii=False),
            hot="true" if caps[n]["hot"] else "false",
            search="true" if caps[n]["search"] else "false",
            creator="true" if caps[n]["creator"] else "false",
            notes=json.dumps(caps[n]["notes"], ensure_ascii=False),
        )
        for n in platform_names
    )
    hot = _platform_sentence(platform_names, caps, "hot", "")
    search = _platform_sentence(platform_names, caps, "search", "搜索")
    creator = _platform_sentence(platform_names, caps, "creator", "创作者查询")
    return f"""{HEADER}
/** 平台清单（PLATFORM_MODULES 注册序）。 */
export const PLATFORMS = [
{platforms_lines}
] as const;

export type PlatformName = (typeof PLATFORMS)[number];

/** 平台能力矩阵（来源：各平台类的 supports_* / capability_notes 声明）。 */
export const PLATFORM_CAPABILITIES: Record<
  PlatformName,
  {{ hot: boolean; search: boolean; creator: boolean; notes: Record<string, string> }}
> = {{
{cap_lines}
}};

/** 统一视频字段清单（来源：platforms/__init__.py 的 VIDEO_FIELDS）。 */
export const VIDEO_FIELDS = [
{fields_lines}
] as const;

/** 字段清单文本（检索工具 describe 的「每项含 …」片段）。 */
export const FIELDS_TEXT = {json.dumps("/".join(video_fields), ensure_ascii=False)};

/** 工具 API 默认值（来源：server/main.py 的 DEFAULT_PLATFORM / DEFAULT_LIMIT）。 */
export const DEFAULT_PLATFORM = {json.dumps(default_platform, ensure_ascii=False)};
export const DEFAULT_LIMIT = {default_limit};

/** 检索工具 describe 的平台句（由能力矩阵生成；行为指导 prose 留在调用点）。 */
const PLATFORM_DESCRIBE: Record<"hot" | "search" | "creator", string> = {{
  hot: {json.dumps(hot, ensure_ascii=False)},
  search: {json.dumps(search, ensure_ascii=False)},
  creator: {json.dumps(creator, ensure_ascii=False)},
}};

export function describePlatformsFor(tool: keyof typeof PLATFORM_DESCRIBE): string {{
  return PLATFORM_DESCRIBE[tool];
}}
"""


def generate() -> str:
    module_names = extract_platform_modules()
    platform_names = [m.split(".")[-1] for m in module_names]
    caps = extract_capabilities(module_names)
    # 校验：能力矩阵键与平台清单一一对应（防声明漏改）
    if set(caps) != set(platform_names):
        raise SystemExit(f"能力声明与平台清单不一致: {sorted(set(platform_names) - set(caps))}")
    video_fields = extract_video_fields()
    default_platform, default_limit = extract_defaults()
    return render(platform_names, caps, video_fields, default_platform, default_limit)


def check() -> int:
    expected = generate()
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == expected:
        print(f"up to date: {OUTPUT}")
        return 0
    print(f"stale: {OUTPUT}（与后端单一来源不一致，请运行 python scripts/gen-tool-schema.py 重新生成）")
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
