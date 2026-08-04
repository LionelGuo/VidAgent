"""LLM 构建：按 .env 配置返回 Agno OpenAIChat。

云端（DeepSeek / 通义千问）与本地（Ollama）均走 OpenAI 兼容协议，单点切换。
"""

from __future__ import annotations

from agno.models.openai import OpenAIChat

from vidagent.config import settings


def build_model() -> OpenAIChat:
    base_url, api_key, model = settings.active_llm()
    # DeepSeek/Ollama 仅认 "system"；Agno 默认把 system 映射成 OpenAI 的 "developer"，需改回。
    return OpenAIChat(
        id=model,
        api_key=api_key,
        base_url=base_url,
        role_map={
            "system": "system",
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
        },
    )
