"""全局配置：通过 .env 读取，是云端/本地 LLM 的单一切换点。"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ----- LLM 提供方（单一切换点）-----
    llm_provider: Literal["cloud", "local"] = "cloud"

    # 云端（OpenAI 兼容协议）
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # 本地（Ollama）
    ollama_base_url: str = "http://localhost:11434/v1"
    llm_model_local: str = "qwen2.5:7b-instruct-q4_K_M"

    # 模型额外参数（JSON 字符串），传给 provider 的 extra_body；
    # 例：关闭 SiliconFlow Qwen3 思考模式 → {"enable_thinking": false}
    llm_extra_body: str = "{}"

    # ----- ASR（Sprint 2 起生效）-----
    whisper_model: str = "base"  # tiny / base / small / medium
    asr_device: str = "auto"  # auto / cuda / cpu

    # ----- 平台 Cookie（可选；公开热门/搜索无需，创作者主页等风控接口需要）-----
    # 形如 "SESSDATA=xxx; bili_jct=xxx; buvid3=xxx"，从浏览器复制
    bili_cookie: str = ""

    # ----- 运行期 -----
    workspace_dir: Path = Path("workspace")

    def active_llm(self) -> tuple[str, str, str]:
        """返回当前生效的 (base_url, api_key, model)。"""
        if self.llm_provider == "local":
            return self.ollama_base_url, "ollama", self.llm_model_local
        return self.openai_base_url, self.openai_api_key, self.llm_model


settings = Settings()
