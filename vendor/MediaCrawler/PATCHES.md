# VidAgent 对 MediaCrawler 的修改（vendor patch 清单）

本目录 vendored 自 [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)
commit `1779dde9725f6b7ef42e29022c0054b3e678f1af`，沿用其《非商业学习许可 1.1》（见 `LICENSE`）。
以下为本工程所作的修改——若未来从上游重新同步，需重放这些补丁：

| # | 文件 | 修改 |
|---|------|------|
| 1 | `config/base_config.py` | 新增 `CDP_DEBUG_HOST = "localhost"` 配置项 |
| 2 | `tools/cdp_browser.py` | 3 处硬编码 `localhost` 改为读取 `config.CDP_DEBUG_HOST`：`_test_cdp_connection` 的 socket 探活、`_get_browser_websocket_url` 的 `/json/version` 请求、`_connect_via_cdp` 的 `ws://` 连接地址 |

**动机**：VidAgent 以 Docker 部署时（尤其 Windows Docker Desktop 桥接网络），容器内
`localhost` 不等于宿主——CDP 需连到宿主 Chrome（`host.docker.internal`）。补丁使宿主可经
`CDP_HOST` 环境变量配置（`src/vidagent/tools/platforms/_cdp_browser.py` 读取）。

**删除的目录**（本工程不需要，未随 vendored 拷贝）：
`api/ cmd_arg/ constant/ webui/ test/ tests/ docs/ data/ workspace/ .github/`
以及 `media_platform/{bilibili,tieba,zhihu}/`、`model/m_{bilibili,weibo,tieba,zhihu}.py`、
`main.py recv_sms.py`、构建/依赖清单文件（`pyproject.toml requirements.txt uv.lock package.json` 等）。

> 注（2026-08-15，#9 微博接入）：`media_platform/weibo/` 7 文件已从上游
> @1779dde **原样恢复**（`model/m_weibo.py` 为空壳仍未恢复——weibo core
> 不使用 dataclass 模型，恢复无意义）。`store/weibo/`、`config/weibo_config.py`
> 自始保留（恢复后成为 weibo core 的 load-bearing 依赖）。
