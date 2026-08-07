#!/usr/bin/env python3
"""Qwen2.5-Omni-7B 推理服务：暴露 OpenAI 兼容的多模态接口。

设计：本机 vidagent 的 _summarize_multimodal 发 OpenAI 格式 payload：
  {"messages": [{"role":"user","content": [
      {"type":"text","text":"..."},
      {"type":"audio_url","audio_url":{"url":"data:audio/mp3;base64,..."}},
      {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}
  ]}]}
本服务把它转成 Qwen2.5-Omni 的格式（audio/image 文件路径），调 generate，
返回 OpenAI /v1/chat/completions 响应。本机只需把 OPENAI_BASE_URL 指向本服务。

环境变量：
  MODEL_REPO   模型 repo（默认 Qwen/Qwen2.5-Omni-7B）
  MODEL_LOCAL  本地下载目录（优先用，避免重复下载）
  QUANT        bnb4(默认,4-bit) / fp16
  PORT         监听端口（默认 6006，AutoDL 自定义服务端口）

启动（服务器上）：
  python serve_omni.py
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("serve_omni")

MODEL_REPO = os.environ.get("MODEL_REPO", "Qwen/Qwen2.5-Omni-7B")
MODEL_LOCAL = os.environ.get("MODEL_LOCAL", "/root/autodl-tmp/Qwen2.5-Omni-7B")
QUANT = os.environ.get("QUANT", "bnb4")
PORT = int(os.environ.get("PORT", "6006"))

model = None
processor = None


# ---------------------------------------------------------------------------
# 模型加载
# ---------------------------------------------------------------------------

def _resolve_repo() -> str:
    if os.path.exists(MODEL_LOCAL) and os.listdir(MODEL_LOCAL):
        return MODEL_LOCAL
    return MODEL_REPO


def load_model():
    global model, processor
    import torch
    from transformers import (
        AutoProcessor,
        BitsAndBytesConfig,
        Qwen2_5OmniForConditionalGeneration,
    )

    repo = _resolve_repo()
    logger.info("加载模型 %s（quant=%s）…", repo, QUANT)
    t0 = time.perf_counter()

    kwargs: dict = {"attn_implementation": "sdpa"}
    if QUANT == "bnb4":
        # 7B 模型: FP16 ~14GB, bnb 4-bit ~3.5GB, 48GB 轻松容纳
        kwargs.update(
            torch_dtype=torch.float16,
            device_map="auto",
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
            ),
        )
    else:  # fp16: ~14GB, 48GB 也轻松
        kwargs.update(
            torch_dtype=torch.float16,
            device_map="auto",
        )

    model = Qwen2_5OmniForConditionalGeneration.from_pretrained(repo, **kwargs)
    processor = AutoProcessor.from_pretrained(repo)
    model.disable_talker()  # 关闭 TTS，纯文本输出

    if torch.cuda.is_available():
        logger.info("GPU 显存已分配: %.1f GB", torch.cuda.memory_allocated(0) / 1e9)

    logger.info("模型加载完成，用时 %.1fs", time.perf_counter() - t0)


# ---------------------------------------------------------------------------
# 多模态输入转换：OpenAI data URL → Qwen 文件路径
# ---------------------------------------------------------------------------

def _b64_to_file(data_url: str, suffix: str) -> str:
    """data:xxx;base64,YYYY → 临时文件，返回路径。"""
    b64 = data_url.split(",", 1)[1]
    data = base64.b64decode(b64)
    p = os.path.join(tempfile.gettempdir(), f"mm_{uuid.uuid4().hex}{suffix}")
    with open(p, "wb") as f:
        f.write(data)
    return p


def _ensure_wav(path: str) -> str:
    """mp3 等非 wav 音频转 16kHz mono wav（Qwen 音频编码器要求）。已是 wav 则原样。"""
    if path.lower().endswith(".wav"):
        return path
    out = path + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", out],
        capture_output=True, timeout=120,
    )
    return out if os.path.exists(out) else path


def convert_content(content):
    """OpenAI content（str 或 parts list）→ Qwen content parts list。返回 (parts, tmp_files)。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}], []

    parts: list[dict] = []
    tmps: list[str] = []
    for part in content:
        t = part.get("type")
        if t == "text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif t == "audio_url":
            url = part["audio_url"]["url"]
            suf = ".wav" if "wav" in url else ".mp3"
            p = _b64_to_file(url, suf)
            p = _ensure_wav(p)
            tmps.append(p)
            parts.append({"type": "audio", "audio": p})
        elif t == "image_url":
            url = part["image_url"]["url"]
            suf = ".png" if "png" in url else ".jpg"
            p = _b64_to_file(url, suf)
            tmps.append(p)
            parts.append({"type": "image", "image": p})
    return parts, tmps


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    model: str = ""
    messages: list[dict]
    temperature: float = 0.3
    max_tokens: int | None = 2048


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok" if model is not None else "loading"}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    from qwen_omni_utils import process_mm_info

    # 1. OpenAI messages → Qwen conversation
    conv: list[dict] = []
    all_tmps: list[str] = []
    for m in req.messages:
        parts, tmps = convert_content(m["content"])
        all_tmps += tmps
        conv.append({"role": m["role"], "content": parts})

    # 2. 预处理为模型输入
    text = processor.apply_chat_template(conv, add_generation_prompt=True, tokenize=False)
    audios, images, videos = process_mm_info(conv, use_audio_in_video=True)
    inputs = processor(
        text=text, audio=audios, images=images, videos=videos,
        return_tensors="pt", padding=True, use_audio_in_video=True,
    )
    inputs = inputs.to(model.device).to(model.dtype)

    # 3. 生成
    t0 = time.perf_counter()
    gen_kwargs = dict(
        return_audio=False, use_audio_in_video=True,
        max_new_tokens=req.max_tokens or 2048,
        do_sample=True, temperature=req.temperature,
    )
    out = model.generate(**inputs, **gen_kwargs)
    # generate 返回结构可能是 tuple / 对象 / tensor，统一处理
    if isinstance(out, tuple):
        out = out[0]
    seqs = out.sequences if hasattr(out, "sequences") else out
    input_len = inputs["input_ids"].shape[1]
    answer = processor.batch_decode(
        seqs[:, input_len:], skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]
    elapsed = time.perf_counter() - t0
    logger.info("生成完成 %d 字符, %.1fs", len(answer), elapsed)

    # 4. 清理临时文件
    for p in all_tmps:
        try:
            os.remove(p)
        except OSError:
            pass

    return {
        "model": req.model or MODEL_REPO,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
