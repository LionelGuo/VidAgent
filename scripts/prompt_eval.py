#!/usr/bin/env python3
"""提示词评测集（提示词与推理过程调优专项，2026-08-15 定案批次①）。

固定用例打前端 /api/chat 全链路（streamText + maxSteps + relay），解析
AI SDK data stream 行协议（`CODE:JSON`：9=tool_call / a=tool_result /
0=text / g=reasoning / d=finish），按用例声明断言「工具选择 / 参数 /
文本启发」，输出判定表与完整转录。

用法（前置：Next dev(:3000) + 后端 + provider 在线；xhs 用例需 Chrome :9222 登录态；
     youtube_creator 用例需后端 .env 配置 YOUTUBE_API_KEY）：
    .venv/bin/python scripts/prompt_eval.py                 # 9 用例 × 2 跑
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

# 内部术语黑名单（B18，用户反馈）：字段名/工具名不得出现在面向用户的
# 回复里（曾「说明：…（字段 duration 为 0）」）。大小写不敏感匹配。
_INTERNAL_TOKENS = (
    "duration", "publish_time", "publish_date", "video_id",
    "duration_text", "view_count", "video_url",
    "get_hot_videos", "search_videos", "get_creator_videos",
    "batch_summarize_videos", "download_video", "extract_and_summarize",
    "multi_part", "entries",
)

# 过时表述形态：「这是/以上是…X月X日(号)…」式声称（B11 回归：曾把条目
# 发布日期误当榜单时效，回复「这是8月8日的数据」）。列表内裸日期不算。
_STALE_CLAIM_RE = re.compile(r"(这是|以上是)[^\n。]{0,10}\d{1,2}月\d{1,2}[日号]")

# 完整日期形态：ISO「2026-08-14」/点号「2026.8.14」或中文「2026年8月14日」，
# 归一为 YYYY-MM-DD 与载荷 publish_date 比对（B17 回归：曾模型自行换算
# 时间戳错年份）。「8月混剪」类无年份表述不匹配——避免标题假阳性。
_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")

# 日期提及（含无年份「8月14日」式）——仅用于判断「回复是否提及日期」，
# 不做载荷比对（无年份无法唯一比对）；「5.4万」类不匹配（须带年月日）。
_DATE_MENTION_RE = re.compile(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}日")

# 「今天是2026年8月15日」式开场——模型报当前日期属正常行为，但当前日期
# 几乎必然不在载荷内，须先剥除再比对（评审：不剥除会假 FAIL）。
_TODAY_PHRASE_RE = re.compile(r"今天是[^\n。]{0,12}?\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")


def parse_data_stream(raw: str) -> dict:
    """解析 AI SDK data stream（行协议 `CODE:JSON`）为结构化事件。

    依赖的行：9=tool_call（toolName/args/toolCallId）、a=tool_result
    （payload 整体保留，供断言结果内容）、0=text 增量（JSON 字符串）、
    g=reasoning 增量、3=error、d=finish。其余行（f/b/c/e/2/8 等）忽略；
    无法解析的行跳过不致命。载荷内部的冒号不破坏切分（按首个冒号 partition）。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results = 0
    tool_result_payloads: list[dict] = []
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
            tool_calls.append({
                "tool": value.get("toolName", ""),
                "args": value.get("args") or {},
                "call_id": value.get("toolCallId"),
            })
        elif code == "a":
            tool_results += 1
            if isinstance(value, dict):
                tool_result_payloads.append(value)
        elif code == "3" and isinstance(value, dict):
            errors.append(json.dumps(value, ensure_ascii=False)[:200])
        elif code == "d" and isinstance(value, dict):
            finish = value
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "tool_result_payloads": tool_result_payloads,
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


def _check_no_internal_jargon(parsed: dict, *, allow_platform_note: bool = False) -> list[str]:
    """B18：回复不得出现字段名/工具名；未问起不得附加平台接口限制注脚。

    allow_platform_note=True 用于「按时长筛选」类用例——该场景说明
    时长缺失是任务本身要求（如实说明义务），接口限制注脚合法。
    """
    text = (parsed.get("text") or "").lower()
    fails = [f"回复出现内部术语 {tok}（应用自然语言表达）" for tok in _INTERNAL_TOKENS if tok in text]
    if not allow_platform_note and "接口限制" in (parsed.get("text") or ""):
        fails.append("回复出现平台接口限制注脚（未问起不应附加说明）")
    return fails


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


def _dates_in_text(text: str) -> list[str]:
    """按出现顺序提取文本中的完整日期，归一为 YYYY-MM-DD。"""
    return [f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in _DATE_RE.findall(text)]


def _verifiable_dates(text: str) -> list[str]:
    """剥除「今天是…」开场后提取可校验的完整日期（防当前日期假 FAIL）。"""
    return _dates_in_text(_TODAY_PHRASE_RE.sub("", text))


def _has_date_mention(text: str) -> bool:
    """回复是否提及了任何日期（完整或「8月14日」式无年份）。"""
    return bool(_DATE_MENTION_RE.search(text))


def _payload_dates(items: list[dict]) -> list[str]:
    """载荷 publish_date 列表（保持载荷顺序；缺失条目为空串）。"""
    return [str(it.get("publish_date", "") or "") for it in items]


def _ordered_in_payload(payload: list[str], mentions: list[str]) -> bool:
    """mentions 是否为 payload 的有序出现序列（允许同一日期被反复提及）。

    与 _has_subsequence 的差异：匹配后不消耗位置——「最新一条发布于
    2026-08-14」+ 列表行再提 08-14 的重复提及不导致假失败（评审：
    共享迭代器逐次消费会把重复提及判败）。
    """
    i = 0
    n = len(payload)
    for t in mentions:
        while i < n and payload[i] != t:
            i += 1
        if i >= n:
            return False
    return True


def _result_items(parsed: dict, tool: str) -> list[dict]:
    """按 call_id 关联工具结果，返回该工具最后一次成功载荷的 results 列表。

    载荷形状（AI SDK data stream + 前端 execute 实测）：
    a:{"toolCallId": ..., "result": {"status": "ok", "results": [...], "count": N}}

    模型对同一工具先失败再重试（CDP 确认框/xhs cookie 过期等瞬态失败）
    会产生多个载荷——绑定最后一条含 results 的载荷，避免把重试前失败
    误归因为「载荷缺 publish_date」（评审：曾只取第一条）。
    """
    call_ids = [tc.get("call_id") for tc in parsed["tool_calls"] if tc["tool"] == tool]
    payloads = parsed.get("tool_result_payloads") or []
    last: list[dict] | None = None
    for call_id in call_ids:
        for pl in payloads:
            if call_id is not None and pl.get("toolCallId") == call_id:
                result = pl.get("result")
                items = result.get("results") if isinstance(result, dict) else None
                if isinstance(items, list):
                    last = items
    return last if last is not None else []


def _check_bili_multi_part_ask(p: dict) -> list[str]:
    """分P先询问（分P专项）：23P 裸 BV → batch 秒回 multi_part → 回复询问。

    断言三件事：① batch 确实被调用且结果含 multi_part 引导（引导链路真实
    发生，而非模型碰巧没总结）；② 无 done 条目（分P未选定不得产出总结）；
    ③ 最终回复说明分P并询问（哪一P/全部）。
    """
    fails = (
        _require_tools(p, ("batch_summarize_videos",))
        + _forbid_tools(p, ("extract_and_summarize",))
        + _check_no_internal_jargon(p)
    )
    statuses = [
        str(r.get("status", ""))
        for r in _result_items(p, "batch_summarize_videos")
    ]
    if "multi_part" not in statuses:
        fails.append(
            f"batch 结果应含 multi_part 引导（BV1bK411W797 为 23P 分P视频），实际 {statuses}"
        )
    if "done" in statuses:
        fails.append("分P未选定不应产出总结（应先询问用户）")
    if "分P" not in p["text"] and "分 P" not in p["text"]:
        fails.append("最终回复应说明这是分P视频")
    if not _ASK_RE.search(p["text"]):
        fails.append("最终回复应询问用户要总结哪一P或全部")
    return fails


# ---------------------------------------------------------------------------
# 用例（check 返回失败列表）
# ---------------------------------------------------------------------------


def _check_hot_bilibili(p: dict) -> list[str]:
    fails = (
        _require_tools(p, ("get_hot_videos",))
        + _forbid_tools(p, _EXECUTE_TOOLS)
        + _check_no_internal_jargon(p)
    )
    args = _first_args(p, "get_hot_videos")
    platform = args.get("platform")
    if platform not in (None, "", "bilibili"):
        fails.append(f"platform 应为 bilibili（或省略走默认），实际 {platform!r}")
    # B11 回归：热榜工具已无 date_filter 参数——传了即旧 schema 未生效
    if "date_filter" in args:
        fails.append(f"热榜工具不应传 date_filter（参数已移除），实际 args={args!r}")
    # B11 回归：不得把条目发布日期误当榜单时效（曾报「这是8月8日的数据」）
    if _STALE_CLAIM_RE.search(p["text"]):
        fails.append("回复出现「这是X月X日的数据」式过时表述（热榜是实时榜单）")
    # B17 顺带加固：模式 A 强制逐行列发布时间，此处钉日期正确性——
    # 载荷缺 publish_date 判败（后端未重启时不可静默跳过，否则回归网失效）；
    # 回复若出现完整日期必须 ⊆ 载荷日期集（热榜条目非按日期排序，只查子集
    # 不查顺序）；「今天是…」开场已剥除（当前日期几乎必然不在载荷内）。
    payload_dates = [d for d in _payload_dates(_result_items(p, "get_hot_videos")) if d]
    if not payload_dates:
        fails.append("载荷缺 publish_date（后端未重启/B17 未生效），无法核验日期")
    elif not _has_date_mention(p["text"]):
        fails.append("回复未列出发布日期（模式 A 要求逐条列发布时间）")
    else:
        for d in _verifiable_dates(p["text"]):
            if d not in set(payload_dates):
                fails.append(f"回复出现载荷之外的日期 {d}（应直接使用 publish_date）")
                break
    if not p["text"].strip():
        fails.append("最终回复为空")
    return fails


def _check_search_python(p: dict) -> list[str]:
    fails = (
        _require_tools(p, ("search_videos",))
        + _forbid_tools(p, _EXECUTE_TOOLS)
        + _check_no_internal_jargon(p)
    )
    keyword = _first_args(p, "search_videos").get("keyword", "")
    if not keyword or "python" not in str(keyword).lower():
        fails.append(f"keyword 应含 python，实际 {keyword!r}")
    return fails


def _check_xhs_creator(p: dict) -> list[str]:
    fails = (
        _require_tools(p, ("get_creator_videos",))
        + _forbid_tools(p, _EXECUTE_TOOLS)
        + _check_no_internal_jargon(p)
    )
    args = _first_args(p, "get_creator_videos")
    if args.get("platform") != "xiaohongshu":
        fails.append(f"platform 应为 xiaohongshu，实际 {args.get('platform')!r}")
    if "影视飓风" not in str(args.get("creator", "")):
        fails.append(f"creator 应含「影视飓风」，实际 {args.get('creator')!r}")
    # B14 回归：结果按发布时间倒序（曾返回 2023 连续旧块）。
    # 结果载荷缺失/为空时直接判败——防假阴（评审修复：曾静默通过）
    items = _result_items(p, "get_creator_videos")
    if not items:
        fails.append("工具结果无载荷或为空，无法验证倒序（应返回影视飓风近期视频）")
        return fails
    times = [int(it.get("publish_time", 0) or 0) for it in items]
    if times != sorted(times, reverse=True):
        fails.append(f"创作者视频应按发布时间倒序，实际 publish_time={times}")
    # 点赞数解析回归：影视飓风视频均有点赞，「X万」字符串曾解析为 0
    zero = [it.get("video_id") for it in items if not it.get("view_count")]
    if zero:
        fails.append(f"存在 view_count=0 的条目（疑似万格式解析回退）: {zero}")
    # B17 回归：模型列出的发布日期必须直接来自载荷 publish_date（有序出现序列，
    # 防串列/换序/自行换算——模型应直接抄字段值；载荷缺字段判败防假阴）
    payload_dates = _payload_dates(items)
    if not any(payload_dates):
        fails.append("载荷缺 publish_date（后端未重启/B17 未生效），无法核验日期")
    elif not _has_date_mention(p["text"]):
        fails.append("回复未列出任何发布日期（应直接使用 publish_date 字段值）")
    else:
        text_dates = _verifiable_dates(p["text"])
        if text_dates and not _ordered_in_payload(payload_dates, text_dates):
            fails.append(
                f"回复日期与载荷不一致（应为载荷 publish_date 的有序子序列）: "
                f"载荷={payload_dates} 回复={text_dates}"
            )
    return fails


def _check_youtube_creator(p: dict) -> list[str]:
    fails = (
        _require_tools(p, ("get_creator_videos",))
        + _forbid_tools(p, _EXECUTE_TOOLS)
        + _check_no_internal_jargon(p)
    )
    args = _first_args(p, "get_creator_videos")
    if args.get("platform") != "youtube":
        fails.append(f"platform 应为 youtube，实际 {args.get('platform')!r}")
    if "3b1b" not in str(args.get("creator", "")).lower():
        fails.append(f"creator 应含 3b1b，实际 {args.get('creator')!r}")
    # B15 回归：曾所有视频时长/播放量全为 0（漏调 videos.list 富化）
    items = _result_items(p, "get_creator_videos")
    if not items:
        fails.append("工具结果为空（应返回 3b1b 的近期视频）")
    for it in items:
        if not it.get("duration"):
            fails.append(f"视频时长不应为 0: {it.get('video_id')} {str(it.get('title'))[:30]!r}")
        if not it.get("view_count"):
            fails.append(f"播放量不应为 0: {it.get('video_id')} {str(it.get('title'))[:30]!r}")
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
    fails = (
        _require_tools(p, ("search_videos",))
        + _forbid_tools(p, _EXECUTE_TOOLS)
        + _check_no_internal_jargon(p, allow_platform_note=True)
    )
    args = _first_args(p, "search_videos")
    if args.get("platform") != "xiaohongshu":
        fails.append(f"platform 应为 xiaohongshu，实际 {args.get('platform')!r}")
    if "时长" not in p["text"]:
        fails.append("最终回复应如实说明时长情况（含「时长」）")
    return fails


def _check_ks_hot_redirect(p: dict) -> list[str]:
    fails = (
        _forbid_tools(p, ("get_hot_videos",))
        + _require_tools(p, ("search_videos",))
        + _check_no_internal_jargon(p)
    )
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
    # B11 回归：复刻实测场景「今天的b站热榜」（曾报「这是8月8日的数据」）
    EvalCase("hot_bilibili", "列出今天的b站热榜", _check_hot_bilibili),
    EvalCase("search_python", "搜Python教程", _check_search_python),
    # xhs 经 MediaCrawler/CDP，耗时较长
    EvalCase("xhs_creator", "列出影视飓风在小红书上发的最新视频", _check_xhs_creator, timeout_s=600),
    # B15 回归：需后端配置 YOUTUBE_API_KEY
    EvalCase("youtube_creator", "3b1b在油管上最近发了哪些视频？", _check_youtube_creator),
    # 真实下载 + 总结，成本最高的用例（定案：1 视频 × 2 跑控成本）
    EvalCase(
        "summarize_hot_first", "总结B站热榜第一个视频", _check_summarize_hot_first, timeout_s=1800
    ),
    EvalCase("ask_missing_param", "帮我总结一下", _check_ask_missing_param),
    # 分P专项：23P 稳定样例（yt-dlp 官方测试视频「物语中的人物是如何吐槽自己的OP的」，
    # 近十年稳定、每P独立标题+91-98s）——断言「先询问、零总结产出」
    EvalCase(
        "bili_multi_part_ask",
        "总结这个B站视频 https://www.bilibili.com/video/BV1bK411W797",
        _check_bili_multi_part_ask,
    ),
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
