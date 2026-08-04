# VidAgent —— 智能自媒体数据采集与多模态分析 Agent 系统

结合开源爬虫与大语言模型，对多平台视频做自动化「采集 → 下载 → 语音转写 → 智能总结」，并用自然语言驱动整条流水线。

> 设计文档见 [`docs/项目开发文档v1.md`](docs/项目开发文档v1.md)，实现规划见 plan。

## 架构（三层解耦）

```
Gradio 对话 UI  →  Agno Agent（意图识别/任务拆解/工具调用）  →  底层 Tools
                                                         ├─ Crawler（B站直连 API / 抖音·XHS 走 MediaCrawler）
                                                         ├─ Downloader（B站 yt-dlp / 抖音·XHS 走 f2）
                                                         └─ Summarizer（ffmpeg 抽音 + faster-whisper ASR + LLM 总结）
```

## 当前进度

- **Sprint 1（进行中）**：B站底层工具打通 —— 热榜/搜索/创作者元数据抓取、无水印下载、音频提取。

## 快速开始

```bash
# 1. 安装依赖（uv）
uv sync                 # 基础依赖（Sprint 1）
uv sync --extra asr     # +ASR（Sprint 2）
uv sync --extra agent --extra ui  # +Agent/UI（Sprint 3）
uv sync --all-extras    # 全量

# 2. 配置 LLM（云端先用）
cp .env.example .env   # 填入 OPENAI_API_KEY 等

# 3. 跑 Sprint 1 验证（B站今日热门 Top5）
uv run python scripts/crawl_cli.py --platform bilibili --task hot_board --limit 5
```

## 技术选型要点

| 关注点 | 选择 | 说明 |
|---|---|---|
| 包管理 | uv + pyproject.toml | 与 MediaCrawler 一致 |
| Agent | Agno（OpenAI 兼容协议） | 云端 DeepSeek / 本地 Ollama 一键切换 |
| B站爬虫 | 直连 REST API（+ WBI 签名） | 公开数据免登录、无 Playwright 开销 |
| 下载 | 按平台分流 | B站 yt-dlp（无水印）；抖音/小红书/快手 f2 |
| ASR | faster-whisper | 显存占用低、速度快 |

## 目录结构

```
src/vidagent/
├── tools/      # Crawler / Downloader / Summarizer（三大 Tool）
├── utils/      # WBI 签名、存储生命周期、显存调度
├── agent.py    # Agno Agent 组装（Sprint 3）
├── ui.py       # Gradio 界面（Sprint 3）
└── pipeline.py # 硬编码流水线（Sprint 2）
scripts/        # CLI 入口
tests/          # 单元测试
```
