# VidAgent —— 智能自媒体数据采集与多模态分析 Agent 系统

自然语言驱动的多平台视频分析：**采集 → 下载 → 多模态总结** 全链路对话式完成。
多模态 LLM（如 Qwen3-Omni）直接聆听音频、观看画面生成结构化总结（无 ASR 中间层）。

## 功能特性

| 能力 | 支持 |
|---|---|
| 平台接入 | **B站 · YouTube · 抖音 · 小红书 · 快手**（搜索 / 热榜 / 创作者） |
| 视频下载 | 无水印，按平台分流（yt-dlp / MediaCrawler CDP 连接现有浏览器） |
| 视频总结 | 多模态模型直送（音频 + 关键帧），短视频 / 长视频双管线（长视频含章节结构化） |
| 对话界面 | Next.js 聊天 UI：流式思考过程、工具调用实时进度、批量总结卡片 |

## 架构

```
Next.js 前端 :3000（AI SDK 流式对话 + 视频卡片 / 总结详情）
   │
   ▼
FastAPI 后端 :8000
   ├─ /v1/chat/completions   SSE Relay（按 provider 预设分流 xml / transparent）
   ├─ /api/tools/{search,hot,creator,download,summarize}   工具 REST + SSE 进度
   └─ 批量总结编排（并行下载 + 总结，失败重试 + 重试端点）
   │
   ├─▶ LLM 提供方（LLM_PROVIDER 单点切换）
   │     ├─ vllm        自托管 vLLM-omni（bare mode，<tool_call> XML 协议）
   │     ├─ siliconflow 远程 API（原生 function calling，audio_url）
   │     └─ generic     任意 OpenAI 兼容端点（原生透传）
   │
   └─▶ 平台层
         ├─ B站 / YouTube   httpx + yt-dlp（WBI 签名 / Data API v3 / JS runtime）
         └─ 抖音 / 小红书 / 快手   MediaCrawler CDP（连接现有 Chrome :9222，页面监听）
```

## 快速开始

> 先试这个：服务跑起来后输入「**搜索 B站 Python 教程并总结第一个**」即可体验完整链路（B站无需任何配置）。

部署分三步：**① 跑起服务**（必做）→ **② 配置平台访问**（按需）→ **③ 本地模型**（可选）。

### ① 跑起服务（Docker，推荐）

```bash
git clone https://github.com/LionelGuo/VidAgent.git && cd VidAgent
cp .env.example .env            # 填 OPENAI_API_KEY（默认 SiliconFlow，填 key 即用）
docker build -t vidagent .
docker run --network=host --env-file .env vidagent
#   浏览器打开 http://localhost:3000
```

`--network=host` 让容器复用宿主 Chrome `:9222`（见②）与 localhost。仅 B站/YouTube 无需 Chrome，
可改桥接 + 端口映射（见「部署」）。裸机开发（非 Docker）亦见「部署」节。

### ② 配置平台访问（按需 —— B站/YouTube 开箱即用；抖音/小红书/快手需一次性配置）

抖音/小红书/快手通过 CDP 复用你**已登录的 Chrome**（不存密码），需一次性配置：
- Windows Chrome 以 `--remote-debugging-port=9222` 启动，并在其中登录对应平台（含 Chrome 146+
  调试模式注意事项，见部署指南故障排查）；
- **Docker 部署需额外设 `CDP_HOST`**：Windows Docker Desktop 桥接网络下容器内 `localhost` 不是宿主，
  在 `.env` 加 `CDP_HOST=host.docker.internal`（裸机/WSL2 部署无需设置）；
- （可选）`.env` 填 `BILI_COOKIE`（B站创作者主页/高清）、`YOUTUBE_API_KEY`（YouTube 检索配额）。

### ③ 本地模型（可选）

默认用 SiliconFlow 等远程 OpenAI 兼容 API（填 `OPENAI_API_KEY` 即用，无需本地 GPU）。有
≥24GB GPU 可自托管 Qwen3-Omni：`bash scripts/deploy_vllm_omni.sh install`（傻瓜脚本）。

## 配置

**后端 `.env`**（模型服务通过 provider 预设切换，差异集中在 `src/vidagent/llm_provider.py`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `vllm` / `siliconflow` / `generic`（`cloud`≡vllm 兼容旧值） | `siliconflow` |
| `OPENAI_API_KEY` | 模型 API 密钥（**必填**） | — |
| `OPENAI_BASE_URL` | OpenAI 兼容端点（留空用 provider 预设默认，如 SiliconFlow 官方端点） | preset |
| `LLM_MODEL` | 模型名（留空用 provider 预设默认） | preset |
| `MULTIMODAL_BASE_URL` / `MULTIMODAL_MODEL` | 多模态端点（留空复用上面的 base_url + model，单端点平台直接留空） | 空 |
| `BILI_COOKIE` | B站 Cookie（含 `SESSDATA`；下载高清流与创作者主页接口均需，避免 CDN 412） | 空 |
| `YOUTUBE_API_KEY` / `YOUTUBE_COOKIE` / `YOUTUBE_PROXY` | YouTube 采集（可选）。`YOUTUBE_PROXY` 同时供抖音等 CDP 平台的部分请求复用 | 空 |
| `MEDIACRAWLER_ROOT` | MediaCrawler 目录（已 vendored 于仓库 `vendor/MediaCrawler/`，见 ADR-0007；可覆盖指向外部副本） | `vendor/MediaCrawler` |
| `WORKSPACE_DIR` | 媒体缓存目录（>7 天自动清理） | `workspace/` |

**前端 `frontend/.env`**（复制 `frontend/.env.example`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `FASTAPI_URL` | 后端地址（Next 服务端 SSE relay 上游） | `http://127.0.0.1:8000` |
| `NEXT_PUBLIC_API_URL` | 后端地址（浏览器直连） | `http://127.0.0.1:8000` |
| `SUMMARY_TIMEOUT_MS` | 批量总结 fetch 超时（毫秒） | `1800000`（30 分钟） |

> **YouTube 下载需要 Node.js ≥ 22**（yt-dlp 2026+ 依赖 JS runtime 解密签名/求解挑战，否则格式退化到 240p）。
> Docker 镜像已内置 node；裸机部署请确认 `node --version ≥ 22`。挑战求解脚本首次使用会从 GitHub 拉取一次（走 `YOUTUBE_PROXY`，缓存于 `~/.cache/yt-dlp`）。

## 部署

### Docker（主逻辑镜像：FastAPI + 前端）

```bash
docker build -t vidagent .
# 场景二：远程 API（最简）
docker run --network=host -e LLM_PROVIDER=siliconflow -e OPENAI_API_KEY=sk-xxx vidagent
# 场景一：配合本地 vLLM（OPENAI_BASE_URL 指向宿主模型服务）
docker run --network=host -e LLM_PROVIDER=vllm -e OPENAI_BASE_URL=http://127.0.0.1:6006/v1 vidagent
```

`--network=host` 推荐：CDP 平台复用宿主 Chrome `:9222`，浏览器直达 localhost。
若宿主 3000/8000 端口被占用（如同时跑开发服务器），可改桥接 + 端口映射：
`docker run -p 18000:8000 -p 13000:3000 -e LLM_PROVIDER=siliconflow -e OPENAI_API_KEY=sk-xxx vidagent`（容器内访问第三方 API 走 NAT 直连）。
抖音/小红书/快手需额外挂载 MediaCrawler（`-e MEDIACRAWLER_ROOT=...`）+ 宿主 Chrome 开调试端口。

### 本地 vLLM-omni 模型服务（场景一，独立部署）

```bash
bash scripts/deploy_vllm_omni.sh install   # 装 vllm-omni + 下载模型（≥24GB VRAM）
bash scripts/deploy_vllm_omni.sh start --bg # 后台启动（端口 6006）
```

### 裸机开发

```bash
uv sync --extra server --extra douyin --extra dev    # 后端依赖
uv run uvicorn server.main:app --host 0.0.0.0 --port 8000   # 后端
cd frontend && npm install && npm run dev            # 前端 :3000
```

进程管理说明：后端 + 前端为两个独立进程，无内置编排。`uv run uvicorn` 是包装进程——直接 kill 它子进程会变孤儿继续占用 8000 端口，**必须按监听 PID 操作**（`ss -tlnp | grep :8000` 拿 PID 再 kill）。生产部署推荐 Docker（入口 `docker/entrypoint.sh` 统一管理）。

## 技术选型要点

| 关注点 | 选择 | 说明 |
|---|---|---|
| 包管理 | uv + pyproject.toml | 与 MediaCrawler 一致；重依赖走 optional extras |
| 后端 | FastAPI + uvicorn | SSE relay（OpenAI 兼容协议转换）+ 工具 REST |
| 前端 | Next.js 15 + AI SDK | 流式对话 + SSE 进度消费，零 UI 库依赖 |
| Agent | AI SDK 工具流（无 agent 框架） | 工具调用经 SSE relay 规范化，双 provider 协议兼容 |
| B站爬虫 | 直连 REST API + WBI 签名 | 公开数据免登录、无 Playwright 开销 |
| YouTube | Data API v3 + yt-dlp | 检索走官方 API；下载含 PO 门控降级链 |
| 抖音/小红书/快手 | MediaCrawler CDP | 连接现有 Chrome，页面监听（不碰签名，规避风控） |
| 下载 | 按平台分流 | B站/YouTube yt-dlp（1080p 封顶）；CDP 平台直链 |
| 总结 | 多模态直送（无 ASR） | 音频 + 关键帧直送 Omni 模型；长短视频双管线 |
| 音频提取 | ffmpeg 子进程 | 比 moviepy 轻 |

## 目录结构

```
server/           FastAPI 后端（main.py 编排 + sse_relay.py 协议转换）
src/vidagent/
├── tools/
│   ├── crawler.py · downloader.py · summarizer.py   检索/下载/总结工具
│   └── platforms/   五平台适配（bilibili/youtube/douyin/xiaohongshu/kuaishou + CDP 共享层）
├── utils/        wbi.py · dates.py · storage.py · audio.py · frames.py · timer.py
├── llm_provider.py   provider 预设系统（端点/relay/媒体格式/推理模式）
└── config.py     配置读取（.env → Pydantic Settings）
frontend/         Next.js 前端（chat 路由 + 组件 + stores）
vendor/MediaCrawler/   抖音/小红书/快手 CDP 平台依赖（vendored 源码，非商用许可，见 NOTICE）
scripts/          deploy_vllm_omni.sh（vLLM-omni 部署）· start_vllm_bare.sh（bare mode 启动）
tests/            pytest（33 passed + 3 xfailed）
```

## 常见问题

- **创作者主页报错 `code=-352` / 非 JSON**：B站风控。在 `.env` 设 `BILI_COOKIE`（浏览器复制含 `SESSDATA` 的 Cookie）。综合热门 / 关键词搜索**无需** Cookie。
- **总结提示「未配置 API key」**：在 `.env` 填 `OPENAI_API_KEY`。
- **下载失败**：yt-dlp 偶发被限流，工具内置随机抖动与降级链；重试即可。
- **YouTube 高清不可用**：确认 `node --version ≥ 22`（JS runtime 必需）；部分视频默认客户端 403 会经 web_embedded 降级链自动重试。
- **工具调用输出 `<tool_call>` 文本而非执行**：`LLM_PROVIDER` 与端点不匹配（vLLM 需 xml 模式）；详见部署指南故障排查。
- **抖音/小红书/快手无结果**：确认 Windows Chrome 带 `:9222` 调试端口运行且平台已登录（快手未登录时 profile 接口返回 `result=109`）。注意：Chrome 146+ 经 chrome://inspect 勾选开启的调试模式**不提供 `/json/*` HTTP 端点**——`curl http://127.0.0.1:9222/json/version` 返回 404 属正常，不代表调试未开启；判断标准：Windows 上 `netstat -ano | findstr :9222` 有 `chrome.exe` LISTENING 即正常。

## 测试

```bash
uv run pytest -q      # 33 passed + 3 xfailed（3 个 xfail 为多模态用例断言形状问题，随视频→总结深模块重构修复后转绿）
uv run ruff check .   # lint
```
