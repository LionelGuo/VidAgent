"""全局配置：通过 .env 读取，是云端/本地 LLM 的单一切换点。"""

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：config.py 位于 src/vidagent/，上溯三级。
# 所有默认路径锚定于此，保证服务进程的 CWD 不影响 workspace/.env 解析
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- LLM 提供方（单一切换点）-----
    # cloud ≡ vllm（自托管 vLLM-omni，兼容旧值）；vllm/siliconflow/generic 走 llm_provider 预设系统；
    # local 仍走 Ollama 旧栈（active_llm）。详见 src/vidagent/llm_provider.py
    llm_provider: Literal["cloud", "local", "vllm", "siliconflow", "generic"] = "cloud"

    # 云端（OpenAI 兼容协议）。留空时由 provider 预设补默认值（如 siliconflow 的官方端点）
    openai_base_url: str = ""
    openai_api_key: str = ""
    llm_model: str = ""

    # 本地（Ollama）
    ollama_base_url: str = "http://localhost:11434/v1"
    llm_model_local: str = "qwen2.5:7b-instruct-q4_K_M"

    # 模型额外参数（JSON 字符串），传给 provider 的 extra_body；
    # 例：关闭 SiliconFlow Qwen3 思考模式 → {"enable_thinking": false}
    llm_extra_body: str = "{}"

    # 多模态推理服务独立端点（需要 audio_url + image_url 支持，如 Qwen3-Omni 系列）
    # 与 agent 模型分离：agent 需要 function calling，多模态不需要
    multimodal_base_url: str = ""
    multimodal_model: str = ""

    # ----- 平台 Cookie / API Key（可选；公开热门/搜索无需，创作者主页等风控接口需要）-----
    # 形如 "SESSDATA=xxx; bili_jct=xxx; buvid3=xxx"，从浏览器复制
    bili_cookie: str = ""
    # YouTube Data API v3 key（可选；无 key 时搜索降级为 yt-dlp ytsearch）
    # 从 https://console.cloud.google.com/apis/credentials 创建，免费配额 10000 units/day
    youtube_api_key: str = ""
    # YouTube 登录 Cookie（可选；从浏览器导出为 Netscape 格式文件路径，或直接填 cookie 字符串）
    # yt-dlp 使用，可绕过部分风控/年龄限制。Netscape 格式：youtube_cookie=/path/to/cookies.txt
    youtube_cookie: str = ""

    # 抖音 Cookie（可选；f2 下载公开视频无需，私密/风控内容需要）
    douyin_cookie: str = ""

    # ----- 网络代理（YouTube / 抖音等需要代理的平台）-----
    # 形如 "http://127.0.0.1:7890"，同时用于 yt-dlp 下载和 API 请求
    youtube_proxy: str = ""

    # ----- 运行期 -----
    workspace_dir: Path = _PROJECT_ROOT / "workspace"

    @field_validator("workspace_dir", mode="after")
    @classmethod
    def _abs_workspace(cls, v: Path) -> Path:
        """相对路径（如 .env 中的 workspace）锚定到项目根目录，与进程 CWD 解耦。"""
        return v if v.is_absolute() else (_PROJECT_ROOT / v).resolve()

    def active_llm(self) -> tuple[str, str, str]:
        """返回当前生效的 (base_url, api_key, model)。"""
        if self.llm_provider == "local":
            return self.ollama_base_url, "ollama", self.llm_model_local
        return self.openai_base_url, self.openai_api_key, self.llm_model


settings = Settings()
