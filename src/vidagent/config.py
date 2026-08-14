"""全局配置：通过 .env 读取，是模型服务的单一切换点。"""

from pathlib import Path

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

    # ----- 模型服务（LLM_PROVIDER 决定其余三项的含义，见 llm_provider.py）-----
    llm_provider: str = "siliconflow"
    # 三项全部必填：端点/密钥/模型名显式配置，切换 provider 时同步修改
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # ----- 平台 Cookie / API Key -----
    bili_cookie: str = ""
    youtube_api_key: str = ""
    youtube_cookie: str = ""
    youtube_proxy: str = ""

    # ----- 运行期 -----
    workspace_dir: Path = _PROJECT_ROOT / "workspace"

    @field_validator("workspace_dir", mode="after")
    @classmethod
    def _abs_workspace(cls, v: Path) -> Path:
        """相对路径锚定到项目根目录，与进程 CWD 解耦。"""
        return v if v.is_absolute() else (_PROJECT_ROOT / v).resolve()


settings = Settings()
