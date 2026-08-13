# VidAgent —— 智能自媒体数据采集与多模态分析 Agent 系统

结合开源爬虫与大语言模型，对多平台视频做自动化「采集 → 下载 → 语音转写 → 智能总结」，并用自然语言驱动整条流水线。

> 设计文档：[`docs/项目开发文档v1.md`](docs/项目开发文档v1.md) ｜ 总结报告：[`docs/实习总结报告.md`](docs/实习总结报告.md)

## 当前进度

| Sprint | 内容 | 状态 |
|---|---|---|
| 1 | B站 底层工具（抓取/下载/抽音） | ✅ |
| 2 | faster-whisper ASR + 云端总结 + 硬编码流水线 | ✅ |
| 3 | Agno Agent + Gradio UI（自然语言驱动） | ✅ |
| 4 | 本地化(Ollama) / 扩平台(抖音·小红书) | 🟡 待启动 |

> 现已可用：**B站 + 云端 DeepSeek** 的完整链路（抓取→下载→ASR→总结），对话式 UI。

## 架构（三层解耦）

```
Gradio 对话 UI
   │  自然语言
   ▼
Agno Agent（意图识别 / 任务拆解 / 工具调用 / 出错反思）
   │  Function Calling
   ▼
底层 Tools
   ├─ Crawler   B站直连 REST API（WBI 签名）· 抖音/XHS 走 MediaCrawler（Sprint4）
   ├─ Downloader B站 yt-dlp（无水印）· 抖音/XHS 走 f2（Sprint4）
   └─ Summarizer ffmpeg 抽音 → faster-whisper ASR → LLM 总结（含无音频降级）
```

## 快速开始

```bash
# 1. 安装依赖（uv）
uv sync --extra asr --extra agent --extra ui   # ASR + Agent + UI
uv sync --extra dev                            # 可选：测试/ lint

# 2. 配置 LLM（复制模板并填 key）
cp .env.example .env        # 至少填 OPENAI_API_KEY（默认指向 DeepSeek）

# 3. 启动对话 UI
uv run python -m vidagent.ui
#   浏览器打开后输入：抓B站今日热榜前3并逐个总结
```

### 命令行（不通过 Agent）

```bash
# 仅抓取元数据
uv run python scripts/crawl_cli.py --task hot_board --limit 5
uv run python scripts/crawl_cli.py --task search --target 大模型 --limit 3

# 抓取 + 下载 + 抽音
uv run python scripts/crawl_cli.py --task hot_board --limit 1 --download

# 硬编码流水线（抓取→下载→ASR→总结，无 Agent）
uv run python -m vidagent.pipeline --task hot_board --limit 1
```

## 配置（`.env`）

模型服务通过 **provider 预设系统**切换（`LLM_PROVIDER`），差异集中在 `src/vidagent/llm_provider.py`。

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `vllm` / `siliconflow` / `generic`（`cloud`≡vllm 兼容旧值；`local`=Ollama 旧栈） | `siliconflow` |
| `OPENAI_API_KEY` | 模型 API 密钥（**必填**） | — |
| `OPENAI_BASE_URL` | OpenAI 兼容端点（留空则用 provider 预设默认，如 SiliconFlow 官方端点） | preset |
| `LLM_MODEL` | 模型名（留空则用 provider 预设默认） | preset |
| `LLM_MULTIMODAL` | `true` 时音频/视频直送全模态模型（Qwen3-Omni），跳过 ASR | `true` |
| `MULTIMODAL_BASE_URL` / `MULTIMODAL_MODEL` | 多模态端点（留空复用上面的 base_url + model，单端点平台直接留空） | 空 |
| `BILI_COOKIE` | B站 Cookie（创作者主页接口需要，含 `SESSDATA`） | 空 |
| `YOUTUBE_API_KEY` / `YOUTUBE_COOKIE` / `YOUTUBE_PROXY` | YouTube 采集（可选） | 空 |
| `MEDIACRAWLER_ROOT` | MediaCrawler 目录（抖音/小红书/快手 CDP 平台需要，默认 `~/Code/MediaCrawler`） | `~/Code/MediaCrawler` |

**两种部署形态：**

- **场景一（本地有 ≥24GB GPU）**：`LLM_PROVIDER=vllm`，先跑模型服务（`scripts/deploy_vllm_omni.sh install && start`），`OPENAI_BASE_URL` 指向它。
- **场景二（远程 API）**：`LLM_PROVIDER=siliconflow`，仅填 `OPENAI_API_KEY`，preset 自动提供端点与模型名。

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
抖音/小红书/快手需额外挂载 MediaCrawler（`-e MEDIACRAWLER_ROOT=...`）+ 宿主 Chrome 开调试端口。

### 本地 vLLM-omni 模型服务（场景一，独立部署）

```bash
bash scripts/deploy_vllm_omni.sh install   # 装 vllm-omni + 下载模型（≥24GB VRAM）
bash scripts/deploy_vllm_omni.sh start --bg # 后台启动（端口 6006）
```

### 裸机开发

```bash
uv sync --extra server --extra asr --extra douyin --extra agent   # 后端依赖
uv run uvicorn server.main:app --host 0.0.0.0 --port 8000         # 后端
cd frontend && npm install && npm run dev                          # 前端 :3000
```

## 技术选型要点

| 关注点 | 选择 | 说明 |
|---|---|---|
| 包管理 | uv + pyproject.toml | 与 MediaCrawler 一致；重依赖走 optional extras |
| Agent | Agno（OpenAI 兼容协议） | 云 DeepSeek / 本地 Ollama 单点切换 |
| B站爬虫 | 直连 REST API + WBI 签名 | 公开数据免登录、无 Playwright 开销 |
| 下载 | 按平台分流 | B站 yt-dlp（无水印）；抖音/小红书/快手 f2 |
| ASR | faster-whisper（ctranslate2） | 显存低、速度快，**不依赖 torch** |
| 音频提取 | ffmpeg 子进程 | 比 moviepy 轻 |

## 目录结构

```
src/vidagent/
├── tools/      bilibili.py·crawler.py·hotboard.py·downloader.py·summarizer.py
├── utils/      wbi.py·dates.py·storage.py·audio.py
├── agent.py    Agno Agent 组装
├── llm.py      build_model() 云/本切换
├── ui.py       Gradio 界面
└── pipeline.py 硬编码流水线
scripts/        crawl_cli.py 采集 CLI
tests/          pytest（18 项）
```

## 常见问题

- **创作者主页报错 `code=-352` / 非 JSON**：B站风控。在 `.env` 设 `BILI_COOKIE`（浏览器复制含 `SESSDATA` 的 Cookie）。综合热门 / 关键词搜索**无需** Cookie。
- **总结提示「未配置 API key」**：在 `.env` 填 `OPENAI_API_KEY`。
- **ASR 显存吃紧**：把 `WHISPER_MODEL` 调小（base→tiny），或 `ASR_DEVICE=cpu`。
- **下载失败**：yt-dlp 偶发被限流，工具内置随机抖动；重试即可。

## 测试

```bash
uv run pytest -q      # 18 项单元测试（纯逻辑，不触网）
uv run ruff check .   # lint
```
