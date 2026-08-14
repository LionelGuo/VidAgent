# VidAgent —— 智能自媒体数据采集与多模态分析 Agent 系统

自然语言驱动的多平台视频分析：**采集 → 下载 → 多模态总结** 全链路对话式完成。
多模态 LLM（如 Qwen3-Omni）直接聆听音频、观看画面生成结构化总结（无 ASR 中间层）。

## 功能特性

| 能力     | 支持                                                                         |
| -------- | ---------------------------------------------------------------------------- |
| 平台接入 | **B站 · YouTube · 抖音 · 小红书 · 快手**（搜索 / 热榜 / 创作者）   |
| 视频下载 | 无水印，按平台分流（yt-dlp / MediaCrawler CDP 连接现有浏览器）               |
| 视频总结 | 多模态模型直送（音频 + 关键帧），短视频 / 长视频双管线（长视频含章节结构化） |
| 对话界面 | Next.js 聊天 UI：流式思考过程、工具调用实时进度、批量总结卡片                |


---

## 快速开始

使用 VidAgent 前，请按以下三步完成基础部署与配置。

<details open>
<summary><b>1. 启动服务</b></summary>

请确保本机已正确配置 [Docker 环境](https://docs.docker.com/engine/install/)。

```bash
# 1. 复制配置文件
cp .env.example .env

# 2. 修改 .env 文件，配置好模型调用接口（详见下方“模型配置参数说明”）

# 3. 构建并启动 Docker 容器
docker build -t vidagent .
docker run -p 8000:8000 -p 3000:3000 --env-file .env vidagent
```

服务启动完成后，在浏览器中打开 [http://localhost:3000](http://localhost:3000) 即可开始体验完整的 Agent 链路。

</details>

<details open>
<summary><b>2. 配置平台访问（按需选择）</b></summary>

VidAgent 支持多平台的数据采集与分析，你可以根据实际需求配置对应的平台授权：

* **B站 (Bilibili)**：需在 `.env` 中填入账号 Cookie，详见下方 [B站 Cookie 获取方法](#b站-cookie-获取方法)。
* **YouTube**：需在 `.env` 中配置对应的 API Key、Cookie 文件路径以及网络代理，详见下方 [YouTube 配置指引](#youtube-配置指引)。
* **抖音 / 小红书 / 快手**：依赖本地浏览器环境。需先 [打开 Chrome 的 Remote Debugging 模式](#chrome-打开-remote-debugging-的方法)，在弹出的安全提示中点击“允许”，并在新打开的浏览器界面中手动登录你自己的平台账号即可。

</details>

<details open>
<summary><b>3. 本地部署模型（可选）</b></summary>

如果本机显存 ≥ 24GB，可以直接在本地私有化部署多模态模型 `Qwen3-Omni-Thinking` 的 4bit 量化版本以获得最佳体验。

项目已内置一键脚本，支持“零参数”串联执行，装完即刻启动：

```bash
# 1. 运行安装脚本下载并配置模型
# 默认下载至 <项目根目录>/models/（内部已锚定脚本位置，不随启动路径漂移）
# 若需自定义路径可执行：bash scripts/deploy_vllm_omni.sh [MODEL_DIR]
bash scripts/deploy_vllm_omni.sh

# 2. 安装完成后，直接启动推理服务（默认读取上述默认路径）
# 若前面自定义了路径，这里需对应执行：bash scripts/start_vllm_bare.sh [MODEL_PATH]
bash scripts/start_vllm_bare.sh
```

服务拉起后，只需将 `.env` 文件中的服务调用端点替换为本地部署的地址即可开始使用（详见下方 [模型配置参数说明](#模型配置参数说明)）。

</details>

---

## 详细配置指南

<details>
<summary><b>模型配置参数说明</b></summary>

在 `.env` 文件中，模型相关的配置主要有以下四项。目前系统支持 `vllm`（本地私有化部署）和 `siliconflow`（云端 API）等模式。切换 `LLM_PROVIDER` 时，请务必同步修改下方对应的三项参数：

```ini
# 模型提供方选项：vllm / siliconflow / generic
LLM_PROVIDER=siliconflow      

# 模型服务密钥
# - siliconflow：填入你在平台申请的 API Key (例如 sk-xxx)
# - vllm：本地自托管模式下可随意填写任意字符串
LLM_API_KEY=sk-xxx            

# 模型服务端点 (Base URL)
# - siliconflow：填写 https://api.siliconflow.cn/v1
# - vllm：填写本地部署的服务端点，例如 http://127.0.0.1:6006/v1
LLM_BASE_URL=https://api.siliconflow.cn/v1   

# 模型名称
# - siliconflow：填写云端模型名称，如 Qwen/Qwen3-Omni-30B-A3B-Thinking
# - vllm：填写本地模型的绝对目录路径
LLM_MODEL=Qwen/Qwen3-Omni-30B-A3B-Thinking   
```

</details>

<details>
<summary><b>B站 Cookie 获取方法</b></summary>

为了能顺利采集部分需登录或防风控的数据（如高清视频流），需要在 `.env` 中配置 `BILI_COOKIE`。请按照以下步骤手动获取：

1. **登录网页版**：在浏览器中打开 [Bilibili 官网](https://www.bilibili.com/) 并登录你的账号。
2. **打开开发者工具**：按下 `F12` 键（或右键点击页面选择“检查”），切换到 **“网络 (Network)”** 面板。
3. **刷新页面**：按下 `F5` 刷新当前网页，让浏览器重新发送请求。
4. **定位请求**：在网络面板的请求列表中，点击任意一个核心请求（通常是最顶部的 `www.bilibili.com` 或 `nav` 接口）。
5. **提取 Cookie**：在右侧的 **“请求头 (Request Headers)”** 区域找到 `Cookie` 字段，将其后方的**整段字符串**复制下来，粘贴到 `.env` 文件中的 `BILI_COOKIE` 字段。

</details>

<details>
<summary><b>YouTube 配置指引</b></summary>

YouTube 的完整使用需要配置 API Key 与 Cookie 文件：

**1. 获取 YouTube API Key**

1. 访问 [Google Cloud Console](https://console.cloud.google.com/) 并登录 Google 账号。
2. 点击顶部导航栏创建或选择一个现有的**项目 (Project)**。
3. 在左侧菜单进入 **“API 和服务 (APIs & Services)”** -> **“库 (Library)”**。
4. 搜索 `YouTube Data API v3`，点击进入并选择 **“启用 (Enable)”**。
5. 返回 **“API 和服务”**，进入 **“凭据 (Credentials)”** 面板。
6. 点击顶部 **“创建凭据 (Create Credentials)”** -> **“API 密钥 (API Key)”**，复制生成的密钥并填入 `.env` 中的 `YOUTUBE_API_KEY`。

**2. 获取并配置 YouTube Cookie**
受限于 YouTube 风控机制，下载功能需要依赖导出的 Cookie 文件：

1. 在 Chrome 网上应用店安装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpocjadpjhebc) 插件。
2. 打开 YouTube 网页并登录你的账号。
3. 点击浏览器右上角的该插件图标，选择 **“Export”** 将 Cookie 导出为 `.txt` 文本文件。
4. 将该文件存放在项目的安全目录中，并在 `.env` 中填入该文件的**绝对路径**（例如 `/path/to/youtube_cookies.txt`）。

*(注：国内网络环境使用 YouTube 功能时，请务必在 `.env` 的 `YOUTUBE_PROXY` 字段配置你的本地代理地址，例如 `http://127.0.0.1:7890`)*

</details>

<details>
<summary><b>Chrome 打开 Remote Debugging 的方法</b></summary>

抖音、小红书、快手等平台的数据采集依赖本地浏览器的 CDP（Chrome DevTools Protocol）调试机制。请按以下步骤配置你的 Chrome：

1. **准备浏览器**：请确保已安装最新版 Chrome 浏览器（**版本需 ≥ 144**，[官方下载地址](https://www.google.com/chrome/)）。
2. **开启调试功能**：在 Chrome 的地址栏中输入 `chrome://inspect/#remote-debugging` 并回车。
3. **授权调试**：在页面中找到并勾选 `Allow remote debugging for this browser instance`（允许调试当前浏览器实例）选项。
4. **验证就绪**：当页面上显示 `Server running at: 127.0.0.1:9222` 时，说明远程调试端口已成功开启。此时在弹出的系统安全提示中点击“允许”，并在当前浏览器界面中直接打开抖音/小红书/快手网页手动登录账号即可。

</details>


## 配置

**后端 `.env`**（模型服务通过 provider 预设切换，差异集中在 `src/vidagent/llm_provider.py`）：

| 变量                                                         | 说明                                                                                                           | 默认            |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- | --------------- |
| `LLM_PROVIDER`                                             | 模型服务切换开关：`vllm`（自托管）/ `siliconflow` / `generic`（任意 OpenAI 兼容端点）                    | `siliconflow` |
| `LLM_API_KEY`                                              | 模型服务密钥（**必填**；`vllm` 自托管可随意填）                                                        | —              |
| `LLM_BASE_URL`                                             | 模型服务端点（**必填**：`vllm` 填自托管端点，`siliconflow` 填 `https://api.siliconflow.cn/v1`）    | —              |
| `LLM_MODEL`                                                | 模型名（**必填**：`vllm` 填本地模型目录路径，`siliconflow` 填 `Qwen/Qwen3-Omni-30B-A3B-Thinking`） | —              |
| `BILI_COOKIE`                                              | B站 Cookie（含 `SESSDATA`；下载高清流与创作者主页接口均需，避免 CDN 412）                                    | 空              |
| `YOUTUBE_API_KEY` / `YOUTUBE_COOKIE` / `YOUTUBE_PROXY` | YouTube 采集（可选）。`YOUTUBE_PROXY` 同时供抖音等 CDP 平台的部分请求复用                                    | 空              |
| `WORKSPACE_DIR`                                            | 媒体缓存目录（>7 天自动清理）                                                                                  | `workspace/`  |

**前端 `frontend/.env`**（复制 `frontend/.env.example`）：

| 变量                    | 说明                                   | 默认                      |
| ----------------------- | -------------------------------------- | ------------------------- |
| `FASTAPI_URL`         | 后端地址（Next 服务端 SSE relay 上游） | `http://127.0.0.1:8000` |
| `NEXT_PUBLIC_API_URL` | 后端地址（浏览器直连）                 | `http://127.0.0.1:8000` |
| `SUMMARY_TIMEOUT_MS`  | 批量总结 fetch 超时（毫秒）            | `1800000`（30 分钟）    |

> **YouTube 下载需要 Node.js ≥ 22**（yt-dlp 2026+ 依赖 JS runtime 解密签名/求解挑战，否则格式退化到 240p）。
> Docker 镜像已内置 node；裸机部署请确认 `node --version ≥ 22`。挑战求解脚本首次使用会从 GitHub 拉取一次（走 `YOUTUBE_PROXY`，缓存于 `~/.cache/yt-dlp`）。

## 技术栈

| 关注点           | 选择                           | 说明                                              |
| ---------------- | ------------------------------ | ------------------------------------------------- |
| 包管理           | uv + pyproject.toml            | 与 MediaCrawler 一致；重依赖走 optional extras    |
| 后端             | FastAPI + uvicorn              | SSE relay（OpenAI 兼容协议转换）+ 工具 REST       |
| 前端             | Next.js 15 + AI SDK            | 流式对话 + SSE 进度消费，零 UI 库依赖             |
| Agent            | AI SDK 工具流（无 agent 框架） | 工具调用经 SSE relay 规范化，双 provider 协议兼容 |
| B站爬虫          | 直连 REST API + WBI 签名       | 公开数据免登录、无 Playwright 开销                |
| YouTube          | Data API v3 + yt-dlp           | 检索走官方 API；下载含 PO 门控降级链              |
| 抖音/小红书/快手 | MediaCrawler CDP               | 连接现有 Chrome，页面监听（不碰签名，规避风控）   |
| 下载             | 按平台分流                     | B站/YouTube yt-dlp（1080p 封顶）；CDP 平台直链    |
| 总结             | 多模态直送（无 ASR）           | 音频 + 关键帧直送 Omni 模型；长短视频双管线       |
| 音频提取         | ffmpeg 子进程                  | 比 moviepy 轻                                     |

## 项目结构

```
server/           FastAPI 后端（main.py 编排 + sse_relay.py 协议转换）
src/vidagent/
├── tools/
│   ├── crawler.py · downloader.py · summarizer.py   检索/下载/总结工具
│   └── platforms/   五平台适配（bilibili/youtube/douyin/xiaohongshu/kuaishou + CDP 共享层）
├── utils/        wbi.py · dates.py · storage.py · audio.py · frames.py · timer.py
├── llm_provider.py   provider 预设系统（relay/媒体格式/推理模式；端点/密钥/模型名由 .env 显式配置）
└── config.py     配置读取（.env → Pydantic Settings）
frontend/         Next.js 前端（chat 路由 + 组件 + stores）
vendor/MediaCrawler/   抖音/小红书/快手 CDP 平台依赖（vendored 源码，非商用许可，见 NOTICE）
scripts/          deploy_vllm_omni.sh（安装 vllm-omni + 模型）· start_vllm_bare.sh（启动服务）
```

## 常见问题

- **创作者主页报错 `code=-352` / 非 JSON**：B站风控。在 `.env` 设 `BILI_COOKIE`（浏览器复制含 `SESSDATA` 的 Cookie）。综合热门 / 关键词搜索**无需** Cookie。
- **总结提示「未配置 API key」**：在 `.env` 填 `LLM_API_KEY`。
- **下载失败**：yt-dlp 偶发被限流，工具内置随机抖动与降级链；重试即可。
- **YouTube 高清不可用**：确认 `node --version ≥ 22`（JS runtime 必需）；部分视频默认客户端 403 会经 web_embedded 降级链自动重试。
- **工具调用输出 `<tool_call>` 文本而非执行**：`LLM_PROVIDER` 与端点不匹配（vLLM 需 xml 模式）；详见部署指南故障排查。
- **抖音/小红书/快手无结果**：确认 Windows Chrome 带 `:9222` 调试端口运行且平台已登录（快手未登录时 profile 接口返回 `result=109`）。注意：Chrome 146+ 经 chrome://inspect 勾选开启的调试模式**不提供 `/json/*` HTTP 端点**——`curl http://127.0.0.1:9222/json/version` 返回 404 属正常，不代表调试未开启；判断标准：Windows 上 `netstat -ano | findstr :9222` 有 `chrome.exe` LISTENING 即正常。
