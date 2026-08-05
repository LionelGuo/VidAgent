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

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `cloud` / `local` | `cloud` |
| `OPENAI_BASE_URL` | 云端 OpenAI 兼容地址 | DeepSeek |
| `OPENAI_API_KEY` | 云端密钥（**必填**） | — |
| `LLM_MODEL` | 云端模型 | `deepseek-chat` |
| `OLLAMA_BASE_URL` / `LLM_MODEL_LOCAL` | 本地 Ollama（Sprint4） | qwen2.5:7b |
| `BILI_COOKIE` | B站登录 Cookie（创作者主页接口需要） | 空 |
| `WHISPER_MODEL` | ASR 模型：tiny/base/small/medium | `base` |
| `ASR_DEVICE` | `auto`/`cuda`/`cpu` | `auto` |

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
- **页面乱码 / 卡在「⏳ 思考中…」/ 只输出残缺工具调用 JSON 文本**：模型选型问题。Agent 依赖**结构化工具调用（function calling）**，必须用原生支持它的非思考（Instruct）模型：
  - ✅ 推荐：`deepseek-ai/DeepSeek-V3`（DeepSeek 官方同族）。实测工具调用稳定、输出干净。
  - ⚠️ 勉强：`Qwen/Qwen2.5-72B-Instruct`（会调用，偶有标残留）。
  - ❌ 避开：`Qwen/Qwen2.5-7B-Instruct`（7B 太小，工具调用能力不足，把 JSON 当文本吐）；以及所有**思考/推理类模型**（如 `Qwen3.X` 带 `reasoning_content`、DeepSeek-R1 等）——思考走 `reasoning_content`、常不支持 tool-calling，与 Agent 不兼容。
  - 经验：Agent 选 ≥32B 的 Instruct 模型，或原生支持 function-calling 者；思考模型和非思考模型区别见 `.env.example`。

## 测试

```bash
uv run pytest -q      # 18 项单元测试（纯逻辑，不触网）
uv run ruff check .   # lint
```
