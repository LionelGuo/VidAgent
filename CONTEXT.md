# VidAgent — 领域术语表 (Ubiquitous Language)

本文档定义项目核心概念，确保代码、文档、讨论中的术语一致。不含实现细节。

## 视频处理流水线

| 术语 | 定义 |
|------|------|
| **视频检索 (Video Retrieval)** | 从平台（B站 / YouTube / 抖音 / 小红书 / 快手）获取视频元数据：热榜、关键词搜索、创作者主页。输出 `video_id + title + desc + video_url` 等结构化数据。 |
| **视频下载 (Video Download)** | 将 `video_url` 指向的视频文件下载到本地 `workspace/`，返回 `local_path`。支持缓存复用。 |
| **音频提取 (Audio Extraction)** | ffmpeg 从视频文件中分离音频轨道，输出 mp3。 |
| **帧抽取 (Frame Extraction)** | ffmpeg 从视频中按均匀间隔抽取关键帧（jpg），帧数 4-16 依时长自适应。帧文件名含秒级时间戳。 |
| **多模态总结 (Multimodal Summarization)** | 音频 + 画面直送全模态模型（Qwen3-Omni），由模型原生理解音视频内容，输出结构化 Markdown 总结，不经 ASR 转写。两条路径：长视频 = 音频 (mp3) + 关键帧 (jpg)；短视频 (<90s) = 音频 + 完整视频 (video_url base64)。 |
| **降级总结 (Fallback Summarization)** | 无音频轨道或模型调用失败时，仅依据元数据（标题+简介）生成总结。 |

## 用户可见的功能产物

| 术语 | 定义 |
|------|------|
| **视频总结 (Video Summary)** | 对单个视频的 Markdown 结构化摘要：核心观点（1-3 条）+ 主要内容梳理 + 关键帧画面描述。在 DetailPanel 中渲染。 |
| **章节时间轴 (Chapter Timeline)** | 长视频总结的章节结构化输出，每章含标题、摘要、`start_time`、`end_time`。渲染为视频进度条上的标记点，对标 B 站「看点」功能。 |

## Agent 架构

| 术语 | 定义 |
|------|------|
| **主 Agent** | 运行在 AI SDK (`streamText`) 中的 LLM，负责意图理解、任务规划、工具调用编排。 |
| **工具 (Tool)** | 主 Agent 可调用的后端能力：检索（`get_hot_videos`, `search_videos`, `get_creator_videos`）、下载（`download_video`）、总结（`extract_and_summarize`, `batch_summarize_videos`）。 |
| **SSE Relay** | 服务端中间层，将 vLLM bare mode 的 `<tool_call>` XML 流转换为 OpenAI 兼容的 `tool_calls` delta，使 AI SDK 能正确解析工具调用。 |
| **Per-task Progress** | 每个视频独立的进度追踪器（`_task_progress[task_id]`），替代全局单例，支持并行总结时各视频独立报告状态。 |

## 部署拓扑

| 术语 | 定义 |
|------|------|
| **vLLM Bare Mode** | Qwen3-Omni-30B-A3B 运行在自托管 GPU 服务器（本地 ≥24GB 或云实例）上，仅提供 `/v1/chat/completions`（无原生 tool_choice）。模型自由输出 `<tool_call>` XML，由 SSE Relay 转换。 |
| **模型提供方 (Provider)** | LLM 服务的来源。本项目支持三种预设：`vllm`（自托管 vLLM-omni，XML 协议）、`siliconflow`（SiliconFlow 平台，原生 function calling）、`generic`（任意标准 OpenAI 兼容端点）。由 `LLM_PROVIDER` 切换，差异集中在 `src/vidagent/llm_provider.py`。 |
| **Provider 预设 (Provider Preset)** | 承载平台差异的三维映射：relay 模式（xml 手写协议 vs 原生透传）、多模态 wire format（input_audio vs audio_url）、推理解析模式（`<think>` 标签 vs reasoning_content 字段）。是迈向「统一 OpenAI API」终态的过渡抽象。 |
| **FastAPI Server** | 本地 `server/main.py`，承担 SSE Relay + 工具 REST API + 静态文件服务。 |
| **Next.js Frontend** | `frontend/` 目录，React 19 + AI SDK v4，承担聊天 UI + DetailPanel + VideoStore 状态管理。 |
| **CDP Browser (Existing)** | 抖音/小红书等平台的客户端通过 CDP 连接 Windows Chrome 的 `:9222` 调试端口（WSL2 localhost 转发），复用用户浏览器登录态（详见 ADR-0004）。 |
