"""章节时间轴管线（#4 深模块：自原 summarizer.py 拆出，C3 将删除）。

候选锚点 + 模型单次约束选择：Phase 1 流式总结 + Phase 2 分段多模态匹配。
注意：自 C7 后服务器入口恒定传入空候选边界（Phase 1 only 模式），
本模块自服务器入口不可达——处置归 #4 Q2（已定案：删除死链）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from vidagent import llm_provider
from vidagent.tools.summarize.multimodal import (
    _MAX_AUDIO_B64_KB,
    _frame_timestamp,
    _get_audio_duration,
    _summarize_multimodal_chunked,
)
from vidagent.tools.summarize.progress import Progress
from vidagent.tools.summarize.transport import _chat_completion, _chat_completion_stream

logger = logging.getLogger(__name__)

_SUMMARY_SYS_CHAPTER = (
    "你是一个专业的视频内容分析师。你会收到视频的完整音频和关键帧画面。\n"
    "请聆听音频、观察画面，然后将视频划分为 3-8 个话题段落。\n\n"
    "输出格式（第一个段落必须是整体概括，其余段落按时间顺序）：\n"
    "## 整体概括\n"
    "用 2-3 句话概括整个视频的主题、内容与核心看点，让读者未读详情即可了解全貌...\n\n"
    "## 开场介绍\n"
    "主持人介绍本期主题和嘉宾背景，现场气氛轻松...\n\n"
    "## 核心讨论\n"
    "三位嘉宾围绕AI伦理展开激烈辩论，主要观点包括...\n\n"
    "## 总结展望\n"
    "主持人对讨论要点进行总结并展望未来趋势...\n\n"
    "关键规则：\n"
    "- **绝对不要输出任何时间戳、秒数或 MM:SS 格式的时间**\n"
    "- 画面标注 [画面 @ Xs] 仅供你理解时间顺序，不要在输出中引用这些数字\n"
    "- 段落按时间先后顺序排列（整体概括除外）\n"
    "- 描述要包含实际内容和关键观点，而非泛泛而谈"
)


def _parse_chapter_response(
    text: str, candidates: list[int],
) -> tuple[list[dict], str]:
    """从模型输出中解析 CHAPTERS JSON 和 SUMMARY Markdown。

    Args:
        text: 模型输出的原始文本（含 <<<CHAPTERS>>> 和 <<<SUMMARY>>> 标记）。
        candidates: 候选边界时间戳列表（用于校验和修正）。

    Returns:
        (chapters: [{start, end, title}], summary_text: str)
        解析失败时 chapters 为空列表，summary_text 为原始文本。
    """
    import json as _json

    chapters: list[dict] = []
    summary_text = text  # 默认返回原始文本

    # ── 提取 CHAPTERS JSON ──
    chap_m = re.search(r"<<<CHAPTERS>>>\s*(.*?)\s*<<<END_CHAPTERS>>>", text, re.DOTALL)
    if chap_m:
        raw_json = chap_m.group(1).strip()
        # 去掉可能的 markdown 代码块包裹 (```json ... ```)
        code_block_m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_json, re.DOTALL)
        if code_block_m:
            raw_json = code_block_m.group(1).strip()
        try:
            data = _json.loads(raw_json)
            # 兼容三种格式：{"chapters": [...]}, [...], 或 {"start":...} 单对象
            if isinstance(data, list):
                raw_chapters = data
            elif isinstance(data, dict):
                # {"chapters": [...]} 或直接是单个章节对象
                raw_chapters = data.get("chapters", [])
                if not raw_chapters and "start" in data:
                    raw_chapters = [data]  # 单个章节对象
            else:
                raw_chapters = []
        except (_json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning("CHAPTERS JSON 解析失败: %s — 原始: %s", e, raw_json[:200])
            raw_chapters = []

        # 校验 + 修正
        for ch in raw_chapters:
            start = int(ch.get("start", 0))
            end = int(ch.get("end", 0))
            title = str(ch.get("title", "")).strip().strip("*").strip()

            # 过滤非法章节
            if start >= end or (end - start) < 10:
                continue
            if not title:
                continue

            # 修正 start/end 到最近的候选边界（±3s 容差）
            def _snap(t: int) -> int:
                for c in candidates:
                    if abs(c - t) <= 3:
                        return c
                # 没匹配到候选值 → 找最近的
                if candidates:
                    return min(candidates, key=lambda c: abs(c - t))
                return t

            start = _snap(start)
            end = _snap(end)

            # 去重：相邻章节 start 相同 → 跳过
            if chapters and chapters[-1]["start"] == start:
                continue

            chapters.append({"start": start, "end": end, "title": title})

        if chapters:
            logger.info(
                "📑 解析章节: %d 个 → %s",
                len(chapters),
                " → ".join(f"{ch['start']}s {ch['title']}" for ch in chapters),
            )
        else:
            logger.warning("CHAPTERS 校验后无有效章节（原始 %d 条）", len(raw_chapters))

    # ── 提取 SUMMARY Markdown ──
    summary_m = re.search(r"<<<SUMMARY>>>\s*(.*?)\s*<<<END_SUMMARY>>>", text, re.DOTALL)
    if summary_m:
        summary_text = summary_m.group(1).strip()

    # ── 回退：尝试从叙述格式中解析章节（如 "**开场介绍** (0-34s):"）──
    if not chapters:
        narrative_pattern = re.findall(
            r"([^(\n]+?)\s*\((\d+)\s*-\s*(\d+)\s*s\)",
            text,
        )
        if narrative_pattern:
            for title, start_str, end_str in narrative_pattern:
                # 清理 markdown 标记和多余空白
                title = title.strip().strip("*").strip()
                start = int(start_str)
                end = int(end_str)
                if start >= end or (end - start) < 10 or not title:
                    continue
                # snap 到候选边界
                def _snap_fb(t: int) -> int:
                    if not candidates:
                        return t
                    return min(candidates, key=lambda c: abs(c - t))
                chapters.append({
                    "start": _snap_fb(start),
                    "end": _snap_fb(end),
                    "title": title,
                })
            if chapters:
                logger.info("📑 从叙述格式解析到 %d 个章节", len(chapters))

    return chapters, summary_text


def _summarize_multimodal_with_chapters(
    mp3_path: Path,
    metadata: dict,
    candidate_boundaries: list[int],
    candidate_frames: list[Path],
    base_url: str,
    api_key: str,
    model: str,
    progress: Progress | None = None,
) -> tuple[list[dict], str]:
    """章节感知的多模态总结：完整音频 + 候选边界帧 → 章节划分 + 时间线总结。

    与 _summarize_multimodal() 的关键区别：
    - 帧前面插入时间戳标注文本
    - System prompt 使用 _SUMMARY_SYS_CHAPTER（含候选边界列表约束）
    - 返回 (chapters, summary_text) 而非纯文本

    长音频（>_MAX_AUDIO_B64_KB）仍走分块路径，但模型在 merge 阶段做章节聚合。
    """
    import base64 as b64

    # Phase 1 only 模式：candidate_boundaries 为空 → 只做总结流式输出
    phase1_only = not candidate_boundaries

    # ── 时长 + 分块决策：基于 base64 大小 ──
    duration = _get_audio_duration(mp3_path)
    mp3_size = mp3_path.stat().st_size
    b64_estimate = int(mp3_size * 4 / 3) // 1024

    if b64_estimate > _MAX_AUDIO_B64_KB:
        # 长音频 → 走 chunked 路径，但改为章节感知的 merge prompt
        logger.info(
            "长音频章节总结：%.0fs / %d KB mp3 → ~%d KB base64 → 分块处理",
            duration, mp3_size // 1024, b64_estimate,
        )
        # 走现有的 chunked 流程，但修改 merge prompt 加入章节约束
        summary_text = _summarize_multimodal_chunked(
            mp3_path=mp3_path, duration=duration,
            metadata=metadata, all_frames=candidate_frames,
            base_url=base_url, api_key=api_key, model=model,
            progress=progress,
        )
        # 尝试从合并后的文本中解析章节（如果 merge prompt 也加了标记）
        chapters, summary = _parse_chapter_response(summary_text, candidate_boundaries)
        if not chapters:
            # 未解析到章节 → 用候选边界做均匀切分作为回退
            logger.warning("长音频未解析到章节，回退为均匀切分")
            chapters = _fallback_chapters(candidate_boundaries, int(duration))
        return chapters, summary

    # ── 短音频：单次请求（流式输出）──
    mp3_b64 = b64.b64encode(mp3_path.read_bytes()).decode()
    audio_b64_kb = len(mp3_b64) // 1024

    # 构建 content_parts：文本提示 + 音频 + 带时间戳标注的帧
    meta_block = ""
    if metadata:
        meta_block = (
            f"【标题】{metadata.get('title', '')}\n"
            f"【简介】{metadata.get('desc', '')}\n"
        )

    prompt_text = (
        f"{meta_block}"
        f"请聆听完整音频，结合关键帧画面，将视频划分为几个话题段落。\n"
        f"在开头先输出一个「## 整体概括」段落（2-3 句话概括全片），"
        f"然后按时间顺序输出各话题段落，每个段落以 ## 标题开头。不要输出任何时间戳。\n"
    )

    content_parts: list[dict] = [
        {"type": "text", "text": prompt_text},
        llm_provider.build_audio_part(mp3_b64),
    ]

    # 帧前面插入时间戳标注（帮助模型关联画面和时点）
    frames_b64_kb = 0
    for f in candidate_frames:
        ts = _frame_timestamp(f)
        ts_label = f"[画面 @ {int(ts)}s]" if ts is not None else "[画面]"
        content_parts.append({"type": "text", "text": ts_label})

        img_b64 = b64.b64encode(f.read_bytes()).decode()
        frames_b64_kb += len(img_b64) // 1024
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}",
                "detail": "low",
            },
        })

    # 使用章节专用的 system prompt
    system_prompt = _SUMMARY_SYS_CHAPTER

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
    }
    payload_kb = len(json.dumps(payload, ensure_ascii=False)) // 1024

    logger.info(
        "📦 发送章节总结请求: 音频 %d KB + %d 帧 / %d KB | "
        "候选边界 %d 个 | payload %d KB → %s",
        audio_b64_kb, len(candidate_frames), frames_b64_kb,
        len(candidate_boundaries), payload_kb, base_url,
    )

    # ── 阶段一：多模态模型流式输出总结（用户实时看到）──
    raw_text = _chat_completion_stream(base_url, api_key, payload, timeout=300, progress=progress)

    # Phase 1 only 模式：不做 Phase 2
    if phase1_only:
        chapters, summary = _parse_chapter_response(raw_text, candidate_boundaries)
        return chapters, summary if summary else raw_text

    # 尝试从 Phase 1 输出直接解析（免费，瞬间完成）
    chapters, summary = _parse_chapter_response(raw_text, candidate_boundaries)

    # ── 阶段二：分段多模态匹配 ──
    # 将音频在候选边界处切开，每段配中间帧，让模型做离散段落选择
    if not chapters and len(candidate_boundaries) >= 3:
        logger.info("📑 阶段二：分段多模态匹配 (%d 段) …", len(candidate_boundaries) - 1)
        chapters = _match_chapters_segmented(
            phase1_summary=summary,
            mp3_path=mp3_path,
            candidate_boundaries=candidate_boundaries,
            candidate_frames=candidate_frames,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    # 兜底：均匀切分
    if not chapters and candidate_boundaries:
        logger.warning("章节提取失败，回退为均匀切分")
        chapters = _fallback_chapters(candidate_boundaries, int(duration))

    return chapters, summary


def _split_audio_at_boundaries(
    mp3_path: Path, boundaries: list[int],
) -> list[Path]:
    """在边界点切开音频，返回段文件路径列表（按时间顺序）。"""
    import subprocess as _sp
    import tempfile
    seg_dir = Path(tempfile.mkdtemp(prefix="vidagent_segs_"))
    # ffmpeg segment: -segment_times 接受逗号分隔的秒数（不含 0 和末尾 duration）
    # 使用 boundaries[1:-1] 确保段数 = len(boundaries) - 1
    if len(boundaries) <= 2:
        return []
    times = ",".join(str(b) for b in boundaries[1:-1])
    _sp.run(
        ["ffmpeg", "-y", "-i", str(mp3_path),
         "-f", "segment", "-segment_times", times,
         "-c:a", "libmp3lame", "-q:a", "7",  # 重编码避免帧边界损坏
         str(seg_dir / "seg_%03d.mp3")],
        capture_output=True, timeout=30,
    )
    segs = sorted(seg_dir.glob("seg_*.mp3"))
    logger.info("✂️ 音频切分为 %d 段 → %s", len(segs), seg_dir)
    return segs


def _match_chapters_segmented(
    phase1_summary: str,
    mp3_path: Path,
    candidate_boundaries: list[int],
    candidate_frames: list[Path],
    base_url: str,
    api_key: str,
    model: str,
) -> list[dict]:
    """阶段二：分段音频 + 帧 → 直接输出 JSON 章节。

    Thinking 模型的 <think> 推理过程替代了旧的自然语言逐段描述，
    最终答案直接是结构化 JSON。
    """
    import base64 as b64
    import json as _json

    # ── 切分音频 ──
    audio_segs = _split_audio_at_boundaries(mp3_path, candidate_boundaries)
    if len(audio_segs) < 2:
        return []

    M = len(audio_segs)  # 段数

    # ── Phase 2 prompt: 直接输出 JSON ──
    prompt = (
        f"以下视频被切分为 {M} 个片段。背景：{phase1_summary[:800]}\n\n"
        f"请逐段聆听音频、观察画面，将片段归并为几个话题章节。\n"
        f"直接输出 JSON 数组（不要代码块、不要解释）：\n"
        f'[{{"title": "开场介绍", "start": 1, "end": 2, "summary": "主持人介绍本期主题和嘉宾"}},\n'
        f' {{"title": "核心讨论", "start": 3, "end": 5, "summary": "嘉宾围绕AI话题展开讨论"}}]\n\n'
        f"规则：\n"
        f"- 段号 1-{M} 必须全部覆盖，相邻章节首尾相接（前一章 end+1 == 后一章 start）\n"
        f"- title 简洁（10 字以内）\n"
        f"- summary 一句话概括本章核心内容（20 字以内）\n"
        f"- 只输出 JSON 数组，不要任何其他文字"
    )

    content_parts: list[dict] = [{"type": "text", "text": prompt}]

    for i in range(M):
        content_parts.append({
            "type": "text",
            "text": f"--- 段{i+1} [{candidate_boundaries[i]}s-{candidate_boundaries[i+1]}s] ---",
        })
        seg_b64 = b64.b64encode(audio_segs[i].read_bytes()).decode()
        content_parts.append(llm_provider.build_audio_part(seg_b64))
        if i < len(candidate_frames):
            img_b64 = b64.b64encode(candidate_frames[i].read_bytes()).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "low"},
            })

    seg_total_kb = sum(s.stat().st_size for s in audio_segs) // 1024
    logger.info("📦 Phase 2: %d 段音频 (%d KB) + %d 帧 → %s", M, seg_total_kb, len(candidate_frames), base_url)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出 JSON 数组。不输出解释、标记或代码块。"},
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.2,
        # max_tokens 由 vLLM --max-num-batched-tokens 统一限制
    }
    phase2_raw = _chat_completion(base_url, api_key, payload, timeout=120)
    phase2_raw = phase2_raw.strip()
    logger.info("📑 Phase 2 输出:\n%s", phase2_raw[:600])

    # ── 清理临时文件 ──
    import shutil
    shutil.rmtree(audio_segs[0].parent, ignore_errors=True)

    # ── 解析 JSON ──
    try:
        # 去掉可能的 markdown 代码块
        raw = phase2_raw
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = data.get("chapters", [])
        if isinstance(data, list) and len(data) > 0:
            chapters: list[dict] = []
            for ch in data:
                # 新格式: {"start": 1, "end": 3, "title": "...", "summary": "..."}
                # 旧格式: {"segments": [1, 2, 3], "title": "..."}
                if "start" in ch and "end" in ch:
                    first = int(ch["start"]) - 1
                    last = int(ch["end"]) - 1
                elif "segments" in ch:
                    segs = ch["segments"]
                    if not segs:
                        continue
                    first = min(segs) - 1
                    last = max(segs) - 1
                else:
                    continue
                if first < 0 or last >= len(candidate_boundaries) - 1:
                    continue
                title = str(ch.get("title", "")).strip()
                chapter = {
                    "start": candidate_boundaries[first],
                    "end": candidate_boundaries[last + 1],
                    "title": title,
                }
                if ch.get("summary"):
                    chapter["summary"] = str(ch["summary"]).strip()
                chapters.append(chapter)
            if chapters:
                logger.info("📑 Phase 2 解析成功: %d 个章节", len(chapters))
                return chapters
    except Exception as e:
        logger.warning("Phase 2 JSON 解析失败: %s", e)

    # ── 回退 ──
    return _fallback_chapters(candidate_boundaries, int(candidate_boundaries[-1]))


def _fallback_chapters(candidates: list[int], duration: int) -> list[dict]:
    """回退：当模型未输出章节时，用候选边界做简单的均匀切分。

    选取候选边界中均匀分布的 3-8 个点作为章节起点。
    """
    if len(candidates) <= 2:
        return []

    target = max(3, min(8, len(candidates) // 2))
    step = max(1, len(candidates) // target)
    selected = candidates[::step]

    chapters: list[dict] = []
    for i, start in enumerate(selected):
        end = selected[i + 1] if i + 1 < len(selected) else duration
        chapters.append({
            "start": start,
            "end": end,
            "title": f"段落 {i + 1}",
        })
    return chapters
