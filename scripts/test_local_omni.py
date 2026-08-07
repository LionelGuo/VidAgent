#!/usr/bin/env python3
"""本地预实验：Qwen2.5-Omni 在 4060 Laptop (8GB) 上跑视频总结。

目的：用真实数据回答
  1. 量化后是否塞得进 8GB 显存？
  2. 吐字速度（TTFT / TPS）如何？对比云端 30B 是否更稳定？
  3. 总结质量能否接受？

依赖（重依赖，未进 pyproject，独立安装）：
  uv pip install torch transformers accelerate
  uv pip install qwen-omni-utils soundfile
  # INT4 量化后端（二选一）：
  uv pip install optimum auto-gptq        # 方案 A
  # 或：uv pip install gptqmodel          # 方案 B

用法：
  uv run python scripts/test_local_omni.py                # 自动选最新 mp4，7B-INT4，combined
  uv run python scripts/test_local_omni.py path/to/video.mp4     # 指定视频
  uv run python scripts/test_local_omni.py --model Qwen/Qwen2.5-Omni-3B   # 换 3B
  uv run python scripts/test_local_omni.py --mode audio          # 仅音频
  uv run python scripts/test_local_omni.py --mode all            # 三组全跑 + 对比

默认模型：Qwen/Qwen2.5-Omni-7B（FP16，~15GB 下载），用 bitsandbytes 4-bit 量化加载，
运行时显存约 5-6GB 权重 + 编码器开销。GPTQ 路径在 transformers 5.x + optimum 2.x 下对
Qwen2.5-Omni 架构不可用（block pattern 检测失败），故默认走 bnb。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vidagent.tools.summarizer import _SUMMARY_SYS  # noqa: E402
from vidagent.utils import frames as frame_utils  # noqa: E402


def _extract_wav(video_path: Path, out_dir: Path | None = None,
                 trim_sec: int | None = None) -> Path:
    """抽取 16kHz 单声道 wav（Qwen 音频编码器的输入格式）。

    trim_sec: 仅取前 N 秒（长音频在 8GB 卡上仍可能因 KV cache 爆显存，截断兜底）。
    """
    out_dir = out_dir or video_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f".{trim_sec}s" if trim_sec else ""
    wav = out_dir / f"{video_path.stem}{suffix}.wav"
    if wav.exists():  # 失败先删，避免读到旧文件
        wav.unlink()
    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    if trim_sec:
        cmd += ["-t", str(trim_sec)]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", str(wav)]
    subprocess.run(cmd, capture_output=True, timeout=120)
    if not wav.exists():
        raise RuntimeError(f"wav 抽取失败: {video_path}")
    return wav


def _load_model(repo: str, disable_talker: bool, quant: str = "bnb"):
    """加载 Qwen2.5-Omni。

    quant:
      - bnb (默认): bitsandbytes 4-bit NF4，对 FP16 模型做内联量化，跨架构最稳。
      - gptq: 直接加载 GPTQ-Int4 repo（transformers 5.x + optimum 2.x 下对 Qwen2.5-Omni
              架构可能因 block pattern 检测失败，仅作备选）。
    """
    import torch  # noqa: F401  确保 torch 可用
    from transformers import AutoProcessor, Qwen2_5OmniForConditionalGeneration

    print(f"⏬ 加载模型 {repo}（quant={quant}）…（首次需下载）")
    t0 = time.perf_counter()

    # attn_implementation=sdpa：编码器默认用 eager(Q×K^T matmul)，长音频下 O(L²) 爆显存。
    # sdpa 融合 kernel 是 O(L) 内存，避免物化完整 attention 矩阵。
    kwargs: dict = {
        "torch_dtype": "auto",
        "device_map": "auto",
        "attn_implementation": "sdpa",
    }
    if quant == "bnb":
        from transformers import BitsAndBytesConfig

        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype="float16",
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
    model = Qwen2_5OmniForConditionalGeneration.from_pretrained(repo, **kwargs)
    processor = AutoProcessor.from_pretrained(repo)
    if disable_talker:
        model.disable_talker()  # 关闭 TTS，省 ~2GB，只保留文本输出
        print("   ✓ 已 disable_talker（纯文本输出）")
    print(f"   ✓ 加载完成，用时 {time.perf_counter() - t0:.1f}s")
    return model, processor


def _build_inputs(processor, model, wav_path: Path, frame_paths: list[Path],
                  mode: str) -> dict:
    """按 mode 构建多模态对话并预处理为模型输入张量。"""
    from qwen_omni_utils import process_mm_info

    user_content: list[dict] = []
    if mode in ("audio", "combined"):
        user_content.append({"type": "audio", "audio": str(wav_path)})
    if mode in ("vision", "combined"):
        for fp in frame_paths:
            user_content.append({"type": "image", "image": str(fp)})

    prompt_text = (
        "请结合音频和关键帧画面，用中文输出结构化总结：\n"
        "1. **核心观点**（1-3 条）\n"
        "2. **主要内容梳理**（按逻辑分点）\n"
        "3. **额外观察**（画面/声音风格，若可感知）"
    ) if mode == "combined" else (
        "请根据提供的内容，用中文输出结构化总结：核心观点 + 主要内容梳理。"
    )
    user_content.append({"type": "text", "text": prompt_text})

    conversation = [
        {"role": "system", "content": [{"type": "text", "text": _SUMMARY_SYS}]},
        {"role": "user", "content": user_content},
    ]

    text = processor.apply_chat_template(
        conversation, add_generation_prompt=True, tokenize=False,
    )
    audios, images, videos = process_mm_info(conversation, use_audio_in_video=True)
    inputs = processor(
        text=text, audio=audios, images=images, videos=videos,
        return_tensors="pt", padding=True, use_audio_in_video=True,
    )
    inputs = inputs.to(model.device)
    try:
        inputs = inputs.to(model.dtype)
    except Exception:
        pass  # INT4 模型 dtype 可能不可直接转换
    return inputs


def _run_once(model, processor, wav_path: Path, frame_paths: list[Path],
              mode: str, max_new_tokens: int = 2048) -> dict:
    """单次推理，测 TTFT / TPS / 显存。用 TextIteratorStreamer 取真实逐 token 时序。"""
    from threading import Thread

    import torch
    from transformers import TextIteratorStreamer

    inputs = _build_inputs(processor, model, wav_path, frame_paths, mode)

    streamer = TextIteratorStreamer(
        processor.tokenizer, skip_prompt=True, skip_special_tokens=True,
    )
    gen_kwargs = dict(inputs, streamer=streamer, max_new_tokens=max_new_tokens)

    torch.cuda.reset_peak_memory_stats()
    t_start = time.perf_counter()
    thread = Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()

    first_t = None
    out_text: list[str] = []
    for tok in streamer:
        if first_t is None and tok.strip():
            first_t = time.perf_counter() - t_start
        out_text.append(tok)
    thread.join()

    elapsed = time.perf_counter() - t_start
    answer = "".join(out_text).strip()
    tok_count = len(processor.tokenizer.encode(answer))
    decode_time = elapsed - (first_t or elapsed)

    return {
        "answer": answer,
        "tokens": tok_count,
        "elapsed_s": round(elapsed, 1),
        "ttft_s": round(first_t, 2) if first_t else None,
        "tps": round(tok_count / decode_time, 1) if decode_time > 0 and tok_count else None,
        "vram_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 2),
    }


def _print_result(r: dict, mode: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"📊 [{mode}] TTFT={r['ttft_s']}s | TPS={r['tps']} | "
          f"总={r['elapsed_s']}s | tokens={r['tokens']} | 显存峰值={r['vram_gb']}GB")
    print(f"{'─' * 50}")
    print(r["answer"] or "(空回答)")
    print(f"{'─' * 50}")


def main():
    parser = argparse.ArgumentParser(description="本地 Qwen2.5-Omni 预实验")
    parser.add_argument("video", nargs="?", type=Path, help="视频路径（默认 workspace 最新 mp4）")
    parser.add_argument("--model", default="Qwen/Qwen2.5-Omni-7B",
                        help="模型 repo（默认 FP16；可换 Qwen/Qwen2.5-Omni-3B）")
    parser.add_argument(
        "--quant", choices=["bnb", "gptq"], default="bnb",
        help="bnb=bitsandbytes 4-bit(默认,稳)；"
             "gptq=GPTQ-Int4(--model 须指向 GPTQ repo)",
    )
    parser.add_argument(
        "--audio-trim-sec", type=int, default=0,
        help="仅取音频前 N 秒（长音频在 8GB 卡上 KV cache 易爆显存；0=不截断）",
    )
    parser.add_argument("--mode", choices=["audio", "vision", "combined", "all"],
                        default="combined")
    parser.add_argument("--frames", type=int, default=8, help="关键帧数上限")
    args = parser.parse_args()

    # 选视频
    video_path: Path | None = args.video
    if video_path is None:
        ws = Path("workspace")
        mp4s = sorted(ws.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mp4s:
            print("❌ workspace 无 mp4，请指定视频路径")
            sys.exit(1)
        video_path = mp4s[0]
    elif not video_path.exists():
        print(f"❌ 文件不存在: {video_path}")
        sys.exit(1)

    print(f"📹 视频: {video_path.name}")
    print(f"   时长: {frame_utils.get_duration(video_path):.0f}s | "
          f"大小: {video_path.stat().st_size / 1024 / 1024:.0f} MB")
    print(f"🤖 模型: {args.model}\n")

    # 预处理
    print("🔧 预处理：抽取 16kHz wav + 自适应关键帧 …")
    trim = args.audio_trim_sec or None
    wav = _extract_wav(video_path, trim_sec=trim)
    print(f"   ✓ 音频: {wav.name} ({wav.stat().st_size / 1024:.0f} KB"
          + (f"，前 {trim}s" if trim else "") + ")")
    all_frames = frame_utils.extract_frames(video_path, num_frames=args.frames)
    print(f"   ✓ 关键帧: {len(all_frames)} 张\n")

    # 加载模型（仅一次，复用）
    model, processor = _load_model(args.model, disable_talker=True, quant=args.quant)

    modes = ["audio", "vision", "combined"] if args.mode == "all" else [args.mode]
    results: list[dict] = []

    for m in modes:
        print(f"\n{'=' * 60}\n▶️  模式: {m}\n{'=' * 60}")
        fps = all_frames if m in ("vision", "combined") else []
        try:
            r = _run_once(model, processor, wav, fps, m)
            _print_result(r, m)
            results.append({"mode": m, **r})
        except Exception as e:
            import traceback
            print(f"❌ [{m}] 失败: {e}")
            traceback.print_exc()
            results.append({"mode": m, "error": str(e)})

    # 汇总
    ok = [r for r in results if "answer" in r]
    if len(ok) >= 1:
        print(f"\n{'=' * 60}\n📊 汇总（{args.model}）\n{'=' * 60}")
        for r in ok:
            print(f"  [{r['mode']}] TTFT={r['ttft_s']}s TPS={r['tps']} "
                  f"显存={r['vram_gb']}GB tokens={r['tokens']}")
        print("\n💡 对比基准（云端 Qwen3-Omni-30B，SiliconFlow）：")
        print("   TTFT ~0.3s | TPS 41-311（波动大）")
        print("   若本地 TPS 稳定且显存 < 8GB → 适合后续 4090 正式部署")


if __name__ == "__main__":
    main()
