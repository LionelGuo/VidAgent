#!/usr/bin/env python3
"""提示词评测集（提示词与推理过程调优专项，2026-08-15 定案批次①）。

固定用例打前端 /api/chat 全链路（streamText + maxSteps + relay），解析
AI SDK data stream 行协议（`CODE:JSON`：9=tool_call / a=tool_result /
0=text / g=reasoning / d=finish），按用例声明断言「工具选择 / 参数 /
文本启发」，输出判定表与完整转录。

用法（前置：Next dev(:3000) + 后端 + provider 在线；xhs 用例需 Chrome :9222 登录态）：
    .venv/bin/python scripts/prompt_eval.py                 # 8 用例 × 2 跑
    .venv/bin/python scripts/prompt_eval.py --filter xhs    # 只跑 id 含 xhs 的用例
    .venv/bin/python scripts/prompt_eval.py --runs 1

判定：两跑全过 = PASS；一过一败 = UNSTABLE（人工审转录）；全败 = FAIL。
转录与摘要落 workspace/prompt-eval/<时间戳>/（workspace/ 已 gitignore）。
不进 CI（依赖活模型，非确定性）；代理环境自动绕过（trust_env=False）。
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:3000"
DEFAULT_OUT = REPO_ROOT / "workspace" / "prompt-eval"

# 检索类用例不应触碰的执行类工具（筛选/列表类请求不得下载或总结）
_EXECUTE_TOOLS = ("download_video", "batch_summarize_videos", "extract_and_summarize")

# 无热榜说明句形态：「(无/没有/非…)热榜/榜单」或「热榜/榜单…搜索」。
# 不能用裸「热榜」子串判定——搜索结果的视频标题可能含「上热榜」类字样（假阳性实例见 20260815-105920 run1）。
_DISCLOSURE_RE = re.compile(
    r"(无|没有|暂无|非|不支持)[^\n。]{0,12}(热榜|榜单)|(热榜|榜单)[^\n。]{0,15}搜索"
)

# 缺参追问形态：疑问式（？/?/哪）或请求式澄清（「请提供/请告诉/请指定/请给出」）。
# 不能只认问号——A6 变体跑出陈述式澄清（「请提供需要总结的具体视频信息…」，
# 功能等价且零工具调用，20260815-114630）。
_ASK_RE = re.compile(r"[？?哪]|请[^\n。]{0,10}(提供|告诉|指定|给出)")


def parse_data_stream(raw: str) -> dict:
    """解析 AI SDK data stream（行协议 `CODE:JSON`）为结构化事件。

    依赖的行：9=tool_call（toolName/args）、a=tool_result、0=text 增量
    （JSON 字符串）、g=reasoning 增量、3=error、d=finish。其余行
    （f/b/c/e/2/8 等）忽略；无法解析的行跳过不致命。载荷内部的冒号
    不破坏切分（按首个冒号 partition）。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results = 0
    errors: list[str] = []
    finish: dict | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        code, _, payload = line.partition(":")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if code == "0" and isinstance(value, str):
            text_parts.append(value)
        elif code == "g" and isinstance(value, str):
            reasoning_parts.append(value)
        elif code == "9" and isinstance(value, dict):
            tool_calls.append({"tool": value.get("toolName", ""), "args": value.get("args") or {}})
        elif code == "a":
            tool_results += 1
        elif code == "3" and isinstance(value, dict):
            errors.append(json.dumps(value, ensure_ascii=False)[:200])
        elif code == "d" and isinstance(value, dict):
            finish = value
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "errors": errors,
        "finish": finish,
    }


# ---------------------------------------------------------------------------
# 断言助手（输入 parse_data_stream 的结果，输出失败描述列表；空列表 = 通过）
# ---------------------------------------------------------------------------


def _tool_names(parsed: dict) -> list[str]:
    return [tc["tool"] for tc in parsed["tool_calls"]]


def _require_tools(parsed: dict, names: tuple[str, ...]) -> list[str]:
    called = _tool_names(parsed)
    return [f"缺少预期工具调用: {n}" for n in names if n not in called]


def _forbid_tools(parsed: dict, names: tuple[str, ...]) -> list[str]:
    called = _tool_names(parsed)
    return [f"出现禁用工具调用: {n}" for n in names if n in called]


def _first_args(parsed: dict, tool: str) -> dict:
    for tc in parsed["tool_calls"]:
        if tc["tool"] == tool:
            return tc["args"]
    return {}


def _has_subsequence(seq: list[str], pattern: list[str]) -> bool:
    """seq 是否按顺序包含 pattern 全部元素（允许中间插入其它元素）。"""
    it = iter(seq)
    return all(x in it for x in pattern)


# ---------------------------------------------------------------------------
# 用例（定案 8 条；check 返回失败列表）
# ---------------------------------------------------------------------------


def _check_hot_bilibili(p: dict) -> list[str]:
    fails = _require_tools(p, ("get_hot_videos",)) + _forbid_tools(p, _EXECUTE_TOOLS)
    platform = _first_args(p, "get_hot_videos").get("platform")
    if platform not in (None, "", "bilibili"):
        fails.append(f"platform 应为 bilibili（或省略走默认），实际 {platform!r}")
    if not p["text"].strip():
        fails.append("最终回复为空")
    return fails


def _check_search_python(p: dict) -> list[str]:
    fails = _require_tools(p, ("search_videos",)) + _forbid_tools(p, _EXECUTE_TOOLS)
    keyword = _first_args(p, "search_videos").get("keyword", "")
    if not keyword or "python" not in str(keyword).lower():
        fails.append(f"keyword 应含 python，实际 {keyword!r}")
    return fails


def _check_xhs_creator(p: dict) -> list[str]:
    fails = _require_tools(p, ("get_creator_videos",)) + _forbid_tools(p, _EXECUTE_TOOLS)
    args = _first_args(p, "get_creator_videos")
    if args.get("platform") != "xiaohongshu":
        fails.append(f"platform 应为 xiaohongshu，实际 {args.get('platform')!r}")
    if "影视飓风" not in str(args.get("creator", "")):
        fails.append(f"creator 应含「影视飓风」，实际 {args.get('creator')!r}")
    return fails


def _check_summarize_hot_first(p: dict) -> list[str]:
    names = _tool_names(p)
    fails = []
    if not _has_subsequence(names, ["get_hot_videos", "batch_summarize_videos"]):
        fails.append(f"应按序调用 get_hot_videos -> batch_summarize_videos，实际 {names}")
    fails += _forbid_tools(p, ("extract_and_summarize", "download_video"))
    return fails


def _check_ask_missing_param(p: dict) -> list[str]:
    fails = []
    if p["tool_calls"]:
        fails.append(f"缺参时应追问而非调用工具，实际调用了 {_tool_names(p)}")
    if not _ASK_RE.search(p["text"]):
        fails.append("最终回复应包含追问（问号/「哪」/「请提供」类请求式）")
    return fails


def _check_xhs_duration_filter(p: dict) -> list[str]:
    fails = _require_tools(p, ("search_videos",)) + _forbid_tools(p, _EXECUTE_TOOLS)
    args = _first_args(p, "search_videos")
    if args.get("platform") != "xiaohongshu":
        fails.append(f"platform 应为 xiaohongshu，实际 {args.get('platform')!r}")
    if "时长" not in p["text"]:
        fails.append("最终回复应如实说明时长情况（含「时长」）")
    return fails


def _check_ks_hot_redirect(p: dict) -> list[str]:
    fails = _forbid_tools(p, ("get_hot_videos",)) + _require_tools(p, ("search_videos",))
    args = _first_args(p, "search_videos")
    if args.get("platform") != "kuaishou":
        fails.append(f"platform 应为 kuaishou，实际 {args.get('platform')!r}")
    # 说明句形态匹配（非裸子串）：见 _DISCLOSURE_RE 注释
    if not _DISCLOSURE_RE.search(p["text"]):
        fails.append("最终回复应说明该平台无热榜并已改用搜索")
    return fails


def _check_capability_chat(p: dict) -> list[str]:
    fails = []
    if p["tool_calls"]:
        fails.append(f"能力介绍不应调用工具，实际调用了 {_tool_names(p)}")
    if not p["text"].strip():
        fails.append("最终回复为空")
    return fails


@dataclass
class EvalCase:
    case_id: str
    user: str
    check: Callable[[dict], list[str]]
    timeout_s: int = 300


CASES = [
    EvalCase("hot_bilibili", "列出B站热榜", _check_hot_bilibili),
    EvalCase("search_python", "搜Python教程", _check_search_python),
    # xhs 经 MediaCrawler/CDP，耗时较长
    EvalCase("xhs_creator", "列出影视飓风在小红书上发的最新视频", _check_xhs_creator, timeout_s=600),
    # 真实下载 + 总结，成本最高的用例（定案：1 视频 × 2 跑控成本）
    EvalCase(
        "summarize_hot_first", "总结B站热榜第一个视频", _check_summarize_hot_first, timeout_s=1800
    ),
    EvalCase("ask_missing_param", "帮我总结一下", _check_ask_missing_param),
    EvalCase(
        "xhs_duration_filter",
        "在小红书搜索旅行攻略，列出时长超过10分钟的",
        _check_xhs_duration_filter,
        timeout_s=600,
    ),
    EvalCase("ks_hot_redirect", "看看快手有什么热门的", _check_ks_hot_redirect),
    EvalCase("capability_chat", "你能做什么？", _check_capability_chat),
]


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def run_once(client: httpx.Client, base: str, case: EvalCase) -> tuple[dict, str, list[str]]:
    """跑单用例一次，返回 (parsed, raw, failures)。"""
    try:
        with client.stream(
            "POST",
            f"{base}/api/chat",
            json={"messages": [{"role": "user", "content": case.user}]},
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(case.timeout_s),
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode(errors="ignore")[:200]
                return {}, f"HTTP {resp.status_code}: {body}", [f"HTTP {resp.status_code}"]
            raw = "".join(resp.iter_text())
    except httpx.HTTPError as e:
        return {}, "", [f"请求失败: {e!r}"]
    parsed = parse_data_stream(raw)
    fails = list(case.check(parsed))
    if parsed["errors"]:
        fails.append(f"流内错误: {parsed['errors']}")
    return parsed, raw, fails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提示词评测集（打前端 /api/chat 全链路）")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"前端地址（默认 {DEFAULT_BASE}）")
    parser.add_argument("--runs", type=int, default=2, help="每用例重复次数（默认 2）")
    parser.add_argument("--filter", default="", help="只跑 case_id 含此子串的用例")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"转录输出目录（默认 {DEFAULT_OUT}）")
    args = parser.parse_args(argv)

    cases = [c for c in CASES if args.filter in c.case_id]
    if not cases:
        parser.error(f"没有匹配的用例: {args.filter!r}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(trust_env=False)
    rows: list[tuple[EvalCase, list[list[str]], str]] = []
    for case in cases:
        run_failures: list[list[str]] = []
        for i in range(1, args.runs + 1):
            parsed, raw, fails = run_once(client, args.base, case)
            (outdir / f"{case.case_id}.run{i}.json").write_text(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "user": case.user,
                        "failures": fails,
                        "parsed": parsed,
                        "raw": raw,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_failures.append(fails)
        if not any(run_failures):
            verdict = "PASS"
        elif all(run_failures):
            verdict = "FAIL"
        else:
            verdict = "UNSTABLE"
        rows.append((case, run_failures, verdict))

    lines = [
        f"# 提示词评测 {stamp}",
        "",
        f"base: `{args.base}` | runs: {args.runs} | filter: `{args.filter or '-'}`",
        "",
        "| 用例 | " + " | ".join(f"run{i}" for i in range(1, args.runs + 1)) + " | 判定 |",
        "|---|" + "---|" * (args.runs + 1),
    ]
    details: list[str] = []
    for case, run_failures, verdict in rows:
        cells = " | ".join("过" if not f else "败" for f in run_failures)
        lines.append(f"| {case.case_id} | {cells} | {verdict} |")
        for i, fails in enumerate(run_failures, 1):
            for f in fails:
                details.append(f"- {case.case_id} run{i}: {f}")
    lines += ["", "## 失败明细", "", *(details or ["（无）"])]
    (outdir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\n转录目录: {outdir}")
    not_pass = [v for _, _, v in rows if v != "PASS"]
    if not_pass:
        print(f"\n{len(not_pass)} 个用例未通过（FAIL/UNSTABLE），详见上方明细与转录")
        return 1
    print("\n全部用例 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
