#!/usr/bin/env python3
"""测试原生多模态模型直接理解视频（不经过 ASR 管道）。

对比三种模态输入的表现：
  - audio      : 抽取音频 → 直接送给多模态模型（测试模型原生音频理解能力）
  - vision     : 抽取关键帧 → 作为图片送给模型（测试纯视觉理解）
  - combined   : 音频 + 关键帧一起送（测试多模态融合）

用法:
  uv run python scripts/test_multimodal_summary.py                      # 自动选 workspace 最新 mp4
  uv run python scripts/test_multimodal_summary.py path/to/video.mp4    # 指定视频
  uv run python scripts/test_multimodal_summary.py --mode audio         # 仅音频
  uv run python scripts/test_multimodal_summary.py --mode vision        # 仅视觉
  uv run python scripts/test_multimodal_summary.py --mode combined      # 音频+视觉
  uv run python scripts/test_multimodal_summary.py --mode all           # 三组全跑 + 对比

不修改主项目任何代码；单独运行、独立评估。
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# 复用项目配置（只读，不写）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vidagent.config import settings  # noqa: E402
from vidagent.utils.audio import extract_audio  # noqa: E402
from vidagent.utils.frames import extract_frames, get_duration  # noqa: E402


# ---------------------------------------------------------------------------
# API 调用
# ---------------------------------------------------------------------------

def _encode_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _api_call(
    content_parts: list[dict],
    label: str,
    timeout: int = 180,
) -> dict:
    """发送一次多模态请求，返回 {label, status, model, elapsed_s, answer, error}。"""
    base_url, api_key, model = settings.active_llm()

    payload: dict = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个专业的视频内容总结助手。请根据提供的多媒体内容，"
                    "用中文输出结构化总结：\n"
                    "1. **核心观点**（1-3 条）\n"
                    "2. **主要内容梳理**（按逻辑分点，简明扼要）\n"
                    "3. **额外观察**（语气、氛围、视觉风格等非语言信息——如果你能感知到的话）\n"
                    "如果输入内容不足以做完整总结，请诚实说明你能理解到什么程度。"
                ),
            },
            {"role": "user", "content": content_parts},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }

    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        if resp.status_code == 200:
            body = resp.json()
            answer = body["choices"][0]["message"]["content"]
            return {
                "label": label,
                "status": "ok",
                "model": model,
                "elapsed_s": round(elapsed, 1),
                "answer": answer,
                "usage": body.get("usage", {}),
            }
        else:
            detail = resp.text[:500]
            return {
                "label": label,
                "status": f"HTTP {resp.status_code}",
                "model": model,
                "elapsed_s": round(elapsed, 1),
                "error": detail,
            }
    except httpx.TimeoutException:
        return {"label": label, "status": "timeout", "elapsed_s": round(time.perf_counter() - t0, 1)}
    except Exception as e:
        return {"label": label, "status": "exception", "elapsed_s": round(time.perf_counter() - t0, 1), "error": str(e)}


# ---------------------------------------------------------------------------
# 三种模态测试
# ---------------------------------------------------------------------------

def test_audio(video_path: Path) -> dict:
    """抽取音频 → 直接发给多模态模型（不经过 ASR）。"""
    print("\n" + "=" * 60)
    print("🎵 测试 1/3：音频直送多模态模型（不经过 Whisper ASR）")
    print("=" * 60)

    t0 = time.perf_counter()
    try:
        mp3 = extract_audio(video_path)
        print(f"   ✓ 音频抽取完成 ({mp3.stat().st_size / 1024:.0f} KB, {time.perf_counter() - t0:.1f}s)")
    except Exception as e:
        print(f"   ✗ 音频抽取失败: {e}")
        return {"label": "audio", "status": "audio_extract_failed", "error": str(e)}

    audio_b64 = _encode_file(mp3)
    print(f"   → base64 编码后 {len(audio_b64) / 1024:.0f} KB，发送中…")

    # 尝试格式 1：audio_url（类 image_url）
    result = _api_call(
        [
            {"type": "text", "text": "请对这段音频内容做中文结构化总结。"},
            {"type": "audio_url", "audio_url": {"url": f"data:audio/mp3;base64,{audio_b64}"}},
        ],
        label="audio (audio_url)",
    )

    # 如果 audio_url 格式失败，尝试 input_audio 格式
    if result["status"] != "ok":
        print(f"   ⚠ audio_url 格式失败: {result['status']}，尝试 input_audio 格式…")
        result = _api_call(
            [
                {"type": "text", "text": "请对这段音频内容做中文结构化总结。"},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}},
            ],
            label="audio (input_audio)",
        )

    _print_result(result)
    return result


def test_vision(video_path: Path, num_frames: int = 8) -> dict:
    """抽取关键帧 → 作为图片发给多模态模型（纯视觉理解）。"""
    print("\n" + "=" * 60)
    print("🖼️  测试 2/3：关键帧 → 视觉模型（不含音频）")
    print("=" * 60)

    t0 = time.perf_counter()
    frames = extract_frames(video_path, num_frames=num_frames)
    if not frames:
        print("   ✗ 无法抽取关键帧")
        return {"label": "vision", "status": "frame_extract_failed"}
    print(f"   ✓ 抽取 {len(frames)} 帧 ({time.perf_counter() - t0:.1f}s)")

    # 构建 image_url 列表 + 文本提示
    content: list[dict] = []
    for f in frames:
        img_b64 = _encode_file(f)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "low"},
        })

    # 把时间戳标注加到文本里
    timestamps = ", ".join(f.stem.replace("frame_", "").replace("_", " ") for f in frames)
    content.append({
        "type": "text",
        "text": (
            f"这是从同一个视频中抽取的 {len(frames)} 张关键帧截图（时间点：{timestamps}），"
            "按时间顺序排列。请用中文做结构化总结：\n"
            "1. 从这些画面中能推断出什么主题/内容？\n"
            "2. 画面风格、场景变化、可能的叙事线索。\n"
            "3. 仅凭画面的局限性（你无法明确知道的）。"
        ),
    })

    print(f"   → {len(frames)} 张图片发送中…")
    result = _api_call(content, label="vision (frames)", timeout=300)
    _print_result(result)
    return result


def test_combined(video_path: Path, num_frames: int = 6) -> dict:
    """音频 + 关键帧一起发给多模态模型。"""
    print("\n" + "=" * 60)
    print("🎵🖼️  测试 3/3：音频 + 关键帧 → 多模态融合")
    print("=" * 60)

    # 音频
    t0 = time.perf_counter()
    try:
        mp3 = extract_audio(video_path)
        print(f"   ✓ 音频抽取完成 ({mp3.stat().st_size / 1024:.0f} KB)")
    except Exception as e:
        print(f"   ✗ 音频抽取失败: {e}")
        return {"label": "combined", "status": "audio_extract_failed", "error": str(e)}

    audio_b64 = _encode_file(mp3)

    # 关键帧（比纯视觉少几张，给音频腾 token）
    frames = extract_frames(video_path, num_frames=num_frames)
    print(f"   ✓ 抽取 {len(frames)} 帧 ({time.perf_counter() - t0:.1f}s)")

    content: list[dict] = []
    # 先放音频
    content.append({
        "type": "audio_url",
        "audio_url": {"url": f"data:audio/mp3;base64,{audio_b64}"},
    })
    # 再放图片
    for f in frames:
        img_b64 = _encode_file(f)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}", "detail": "low"},
        })
    content.append({
        "type": "text",
        "text": (
            f"以上是同一个视频的音频和 {len(frames)} 张关键帧截图。"
            "请结合声音和画面，用中文做结构化总结：\n"
            "1. 核心观点 2. 主要内容梳理 3. 画面/声音风格等额外观察。"
        ),
    })

    print(f"   → 音频 + {len(frames)} 张图片发送中…")
    result = _api_call(content, label="combined (audio+frames)", timeout=300)

    # fallback: 如果 audio_url 失败，尝试 input_audio
    if result["status"] != "ok" and "audio_url" in str(result.get("error", "")):
        content[0] = {
            "type": "input_audio",
            "input_audio": {"data": audio_b64, "format": "mp3"},
        }
        print("   ⚠ 换用 input_audio 格式重试…")
        result = _api_call(content, label="combined (input_audio+frames)", timeout=300)

    _print_result(result)
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _print_result(r: dict) -> None:
    if r["status"] == "ok":
        print(f"\n{'─' * 50}")
        print(f"✅ [{r['label']}] 成功 | 模型 {r.get('model','')} | {r['elapsed_s']}s")
        if "usage" in r:
            u = r["usage"]
            print(f"   tokens: prompt={u.get('prompt_tokens','?')}  completion={u.get('completion_tokens','?')}")
        print(f"{'─' * 50}")
        print(r["answer"])
        print(f"{'─' * 50}")
    else:
        print(f"\n❌ [{r['label']}] {r['status']} | {r.get('elapsed_s','?')}s")
        if r.get("error"):
            print(f"   {r['error'][:400]}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="测试原生多模态模型直接总结视频（不经过 ASR）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "video", nargs="?", type=Path,
        help="视频文件路径（默认：workspace 中最新的 mp4）",
    )
    parser.add_argument(
        "--mode", choices=["audio", "vision", "combined", "all"], default="all",
        help="测试模式 (default: all — 三组全跑)",
    )
    parser.add_argument(
        "--frames", type=int, default=8,
        help="关键帧数量 (default: 8)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预处理（抽取音频/帧），不发 API 请求",
    )
    args = parser.parse_args()

    # 选视频
    video_path: Path | None = args.video
    if video_path is None:
        ws = Path(settings.workspace_dir)
        mp4s = sorted(ws.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mp4s:
            print("❌ workspace 无 mp4 文件，请指定视频路径")
            sys.exit(1)
        video_path = mp4s[0]
        print(f"📹 自动选择最新视频: {video_path.name}")
    elif not video_path.exists():
        print(f"❌ 文件不存在: {video_path}")
        sys.exit(1)

    print(f"   时长: {get_duration(video_path):.0f}s | 大小: {video_path.stat().st_size / 1024 / 1024:.0f} MB")

    # 配置信息
    base_url, api_key, model = settings.active_llm()
    print(f"   模型: {model}")
    print(f"   端点: {base_url}")
    if not api_key:
        print("❌ 未配置 API key（OPENAI_API_KEY）")
        sys.exit(1)

    if args.dry_run:
        print("\n🔍 --dry-run：仅预处理，不发 API\n")
        mp3 = extract_audio(video_path)
        print(f"   音频: {mp3} ({mp3.stat().st_size / 1024:.0f} KB)")
        frames = extract_frames(video_path, args.frames)
        print(f"   关键帧: {len(frames)} 张")
        for f in frames:
            print(f"     {f.name} ({f.stat().st_size / 1024:.0f} KB)")
        return

    results: list[dict] = []

    if args.mode in ("audio", "all"):
        results.append(test_audio(video_path))

    if args.mode in ("vision", "all"):
        results.append(test_vision(video_path, args.frames))

    if args.mode in ("combined", "all"):
        results.append(test_combined(video_path, max(args.frames - 2, 4)))

    # 汇总
    if len(results) >= 2:
        print("\n" + "=" * 60)
        print("📊 汇总对比")
        print("=" * 60)
        ok = [r for r in results if r["status"] == "ok"]
        fail = [r for r in results if r["status"] != "ok"]
        print(f"   成功: {len(ok)}/{len(results)}  ({', '.join(r['label'] for r in ok) or '无'})")
        if fail:
            for r in fail:
                print(f"   ❌ {r['label']}: {r['status']}")
        if ok:
            for r in ok:
                tokens = r.get("usage", {}).get("total_tokens", "?")
                print(f"   [{r['label']}] {r['elapsed_s']}s | {tokens} tokens")
            print("\n💡 提示：如果多模态模型能直接理解音频，则可以省去 ASR 步骤。")


if __name__ == "__main__":
    main()
