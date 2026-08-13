"""LLM 提供方预设与端点解析（单一真实来源）。

集中处理多平台差异，让上层调用者无需感知具体 provider：
- 自托管 vLLM-omni（bare mode，<tool_call> XML 协议，input_audio wire format，<think> 标签推理）
- SiliconFlow（标准 OpenAI 兼容，原生 function calling，audio_url wire format，reasoning_content 流）
- 通用 OpenAI 兼容端点（原生透传，保守假设 vllm-omni 类格式）

四个维度的差异：
  relay_mode      — agent 工具调用：xml 手写协议转换 vs 原生透传
  media_format    — 多模态 part：input_audio vs audio_url
  reasoning_mode  — 推理内容解析：<think> 标签内联 vs delta.reasoning_content 独立字段

agent（对话+工具）与多模态（总结）端点可分离配置（MULTIMODAL_* 显式优先）；
未配置多模态时回退到 agent 端点（单端点平台如 SiliconFlow 开箱即用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vidagent.config import settings

RelayMode = Literal["xml", "transparent"]
MediaFormat = Literal["vllm", "siliconflow"]
ReasoningMode = Literal["think_tag", "reasoning_content"]


@dataclass(frozen=True)
class ProviderPreset:
    """provider 预设：默认端点 + 三维行为映射。显式环境变量可覆盖 base_url/model。"""

    name: str
    default_base_url: str
    default_model: str
    relay_mode: RelayMode
    media_format: MediaFormat
    reasoning_mode: ReasoningMode


_PRESETS: dict[str, ProviderPreset] = {
    # 自托管 vLLM-omni：bare mode 无原生 function calling → XML 协议转换；input_audio wire format
    "vllm": ProviderPreset("vllm", "", "", "xml", "vllm", "think_tag"),
    # SiliconFlow：标准 OpenAI 兼容 + 原生 function calling；audio_url；reasoning_content 流
    "siliconflow": ProviderPreset(
        "siliconflow",
        "https://api.siliconflow.cn/v1",
        "Qwen/Qwen3-Omni-30B-A3B-Thinking",
        "transparent",
        "siliconflow",
        "reasoning_content",
    ),
    # 通用 OpenAI 兼容端点：原生透传；保守假设 vllm-omni 类格式（input_audio / <think>）
    "generic": ProviderPreset("generic", "", "", "transparent", "vllm", "think_tag"),
}

# 旧值兼容：cloud ≡ vllm（自托管 OpenAI 兼容端点，即 vLLM-omni）
_PROVIDER_ALIASES = {"cloud": "vllm"}


@dataclass(frozen=True)
class Endpoint:
    """解析后的端点配置（base_url + key + model + 行为维度）。"""

    base_url: str
    api_key: str
    model: str
    media_format: MediaFormat
    reasoning_mode: ReasoningMode


def _normalize_provider() -> str:
    """规范化 llm_provider 到 preset 名（cloud→vllm）。local 保持 local（走旧栈）。"""
    return _PROVIDER_ALIASES.get(settings.llm_provider, settings.llm_provider)


def _preset() -> ProviderPreset:
    """当前 provider 的预设（local/未知 → generic 兜底）。"""
    return _PRESETS.get(_normalize_provider(), _PRESETS["generic"])


def is_legacy_local() -> bool:
    """是否走 Ollama 旧栈（LLM_PROVIDER=local）。"""
    return settings.llm_provider == "local"


def agent_endpoint() -> Endpoint:
    """agent（对话 + 工具调用）端点。relay 按 relay_mode() 分流。"""
    if is_legacy_local():
        base_url, api_key, model = settings.active_llm()
        return Endpoint(base_url, api_key, model, "vllm", "think_tag")
    preset = _preset()
    base_url = settings.openai_base_url or preset.default_base_url
    model = settings.llm_model or preset.default_model
    return Endpoint(
        base_url, settings.openai_api_key, model, preset.media_format, preset.reasoning_mode
    )


def multimodal_endpoint() -> Endpoint:
    """多模态总结端点：MULTIMODAL_* 显式优先，回退到 agent 端点配置（单端点平台直接可用）。"""
    if is_legacy_local():
        base_url, api_key, model = settings.active_llm()
        return Endpoint(base_url, api_key, model, "vllm", "think_tag")
    preset = _preset()
    base_url = (
        settings.multimodal_base_url or settings.openai_base_url or preset.default_base_url
    )
    model = (
        settings.multimodal_model or settings.llm_model or preset.default_model
    )
    return Endpoint(
        base_url, settings.openai_api_key, model, preset.media_format, preset.reasoning_mode
    )


def relay_mode() -> RelayMode:
    """agent 工具调用 relay 工作模式（xml / transparent）。"""
    if is_legacy_local():
        return "xml"
    return _preset().relay_mode


# ── 多模态 wire format 适配 ──────────────────────────────────────────────────


def build_audio_part(mp3_b64: str, media_format: MediaFormat | None = None) -> dict:
    """音频 content part。vLLM-omni: input_audio；SiliconFlow: audio_url（标准 OpenAI 无 input_audio）。"""
    fmt = media_format if media_format is not None else multimodal_endpoint().media_format
    if fmt == "siliconflow":
        return {"type": "audio_url", "audio_url": {"url": f"data:audio/mp3;base64,{mp3_b64}"}}
    return {"type": "input_audio", "input_audio": {"data": mp3_b64, "format": "mp3"}}


def build_video_part(
    video_b64: str, mime: str = "video/mp4", media_format: MediaFormat | None = None
) -> dict:
    """视频 content part。vLLM-omni: video_url（引擎级抽帧）；SiliconFlow: video_url + max_frames/fps（服务端抽帧）。"""
    fmt = media_format if media_format is not None else multimodal_endpoint().media_format
    if fmt == "siliconflow":
        return {
            "type": "video_url",
            "video_url": {
                "url": f"data:{mime};base64,{video_b64}",
                "detail": "low",
                "max_frames": 16,
                "fps": 1,
            },
        }
    return {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{video_b64}"}}
