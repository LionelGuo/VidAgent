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

    # 多模态模型开关：开启后 extract_and_summarize 跳过 ASR，音频直送 LLM
    # 需要模型支持 audio_url（如 Qwen3-Omni 系列）
    llm_multimodal: bool = False

    # 多模态推理服务独立端点（需要 audio_url + image_url 支持，如 Qwen2.5-Omni-7B）
    # 与 agent 模型分离：agent 需要 function calling，多模态不需要
    multimodal_base_url: str = ""
    multimodal_model: str = ""

    # ----- ASR（Sprint 2 起生效）-----
    whisper_model: str = "base"  # tiny / base / small / medium
    asr_device: str = "auto"  # auto / cuda / cpu

    # ----- 平台 Cookie / API Key（可选；公开热门/搜索无需，创作者主页等风控接口需要）-----
    # 形如 "SESSDATA=xxx; bili_jct=xxx; buvid3=xxx"，从浏览器复制
    bili_cookie: str = ""
    # YouTube Data API v3 key（可选；无 key 时搜索降级为 yt-dlp ytsearch）
    # 从 https://console.cloud.google.com/apis/credentials 创建，免费配额 10000 units/day
    youtube_api_key: str = ""
    # YouTube 登录 Cookie（可选；从浏览器导出为 Netscape 格式文件路径，或直接填 cookie 字符串）
    # yt-dlp 使用，可绕过部分风控/年龄限制。Netscape 格式：youtube_cookie=/path/to/cookies.txt
    youtube_cookie: str = ""

    # ----- 网络代理（YouTube 等需要科学上网的平台）-----
    # 形如 "http://127.0.0.1:7890"，同时用于 yt-dlp 下载和 API 请求
    youtube_proxy: str = ""

    # ----- 运行期 -----
    workspace_dir: Path = Path("workspace")

    def active_llm(self) -> tuple[str, str, str]:
        """返回当前生效的 (base_url, api_key, model)。"""
        if self.llm_provider == "local":
            return self.ollama_base_url, "ollama", self.llm_model_local
        return self.openai_base_url, self.openai_api_key, self.llm_model


settings = Settings()
