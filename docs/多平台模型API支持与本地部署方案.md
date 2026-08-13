# 多平台模型 API 支持与本地部署方案

> 状态：grilling 决策完成，待确认后实施
> 日期：2026-08-13
> 议题背景：开源分发前置条件——用户不一定使用自托管 vLLM-omni 服务，需支持标准 OpenAI 兼容第三方平台（以 SiliconFlow 同款模型 Qwen/Qwen3-Omni-30B-A3B-Thinking 为实验对象）。同时，交付需求要求「本地部署」为默认形态（主逻辑 Docker 镜像 + vLLM 独立部署脚本），远程 API 为性能不足用户的退化方案。

## 决策记录（grilling 全轮）

| # | 议题 | 决策 |
|---|------|------|
| Q1 | 交付形态 | B：本 session 实施 + SiliconFlow 端到端实测 |
| Q2 | Provider 配置形态 | B：preset + 覆盖（阶段性折中，终态 = 统一 OpenAI API，等 vllm-omni 上游成熟后删代码收敛） |
| Q3 | 思考降级 | A：能力声明自动适配；红线——完全不影响现有 vLLM 部署正常功能 |
| Q5 | agent 工具调用 | A：relay 分流（vllm 走 XML 转换原样保留；siliconflow 走原生透传；模型名服务端注入；前端单代码路径） |
| Q6 | 多模态抽象 | A：provider 适配层（中性 media parts → wire format 映射）+ 收敛内联配置为共享客户端工厂 |
| Q7 | API key | B：单一全局 `OPENAI_API_KEY`（切 provider 即换 key） |
| Q8 | 旧栈残留 | A：本议题不动（`cloud|local` 旧值语义兼容），C7 仍为独立任务 |
| Q9 | SiliconFlow 变体 | A：`-Thinking` 纯思考变体（不传 enable_thinking，规避官方文档矛盾区） |
| Q10 | 文档交付物 | A：全套（.env.example / README / CONTEXT.md / 本方案 / ADR-0005） |
| Q11 | 实测方案 | A：分层实测（① HTTP 冒烟 ② 代码改造 ③ 硅流端到端 ④ vLLM 回归 ⑤ 镜像冒烟 ⑥ 脚本片段验证） |
| Q12 | 部署拓扑 | 用户自定义：Docker 镜像 = 主逻辑（FastAPI+前端）；vLLM = 独立服务 + 独立脚本/文档；配置够 → docker + 本地模型；不够 → docker + API key |
| Q13 | 部署脚本时序 | B：本 session 产出 + 现有 AutoDL 无损片段验证；发布前在全新 24GB 机器完整验证（进检查清单） |
| Q14 | 镜像形态 | A：单镜像（容器内编排 FastAPI :8000 + Next 生产构建 :3000） |
| Q15 | 镜像验证深度 | A：裸机调试 + 镜像内「场景二」冒烟 |
| Q16 | 镜像 ASR 依赖 | A：携带完整依赖（镜像行为 = 本机行为） |

## 关键事实基础（调研结论）

1. **HTTP 层已统一**：vllm-omni 与 SiliconFlow 均为标准 `/v1/chat/completions`，openai SDK 直连可用。差异仅三处：工具调用机制（vllm-omni 原生 function calling 不可用 → bare mode XML 协议；SiliconFlow 原生支持，社区实践建议 `stream=True + modalities=["text"]`）、多模态 wire format（`input_audio` vs `audio_url`；抽帧参数引擎级 vs 每请求 `max_frames/fps`）、思考参数（`chat_template_kwargs.enable_thinking` vs 顶层 `enable_thinking`）。
2. **SiliconFlow**：`https://api.siliconflow.cn/v1`；`reasoning_content` 流式字段与 vLLM 一致；视频输入为自家扩展 `video_url(max_frames/fps)`（**服务端抽帧，无法从视频取音轨** → 必须继续单独传音频）；视频建议 ≤30s；上下文约 66K；官方文档对 Omni 的思考参数自相矛盾（API reference 有 `enable_thinking`，reasoning 文档只提 `thinking_budget`）→ 实测确认。
3. **现状痛点**：前端模型名硬编码（route.ts:176）；relay 只读 `delta.content`（会丢弃 reasoning_content）；多模态 3 处内联重复配置、无共享工厂；短视频路径 `video_url` 为 vLLM 特有 content 类型。
4. **本机硬件**：RTX 4060 8GB（WSL2 + Docker 29.4）——无法本地跑 18GB AWQ 权重；主逻辑镜像可本机完整验证，模型部署脚本验证需 AutoDL（24GB 基线）。

## 架构设计

### 1. Provider 预设系统（config.py）

- `LLM_PROVIDER = vllm | siliconflow | generic`（新增值；旧值 `cloud|local` 保留兼容：`cloud` ≡ `vllm` 别名，`local` ≡ ollama 不动）
- preset 数据结构（dataclass）：默认 base_url、agent 模型、多模态模型、media wire format 映射、思考参数映射、能力声明
  - `vllm`：base_url=`OPENAI_BASE_URL`（显式）、模型=`LLM_MODEL`/`MULTIMODAL_MODEL||LLM_MODEL`、key=`OPENAI_API_KEY`、media=`input_audio`+`video_url`（**现状逐字节等价**）、thinking=不传参（`<think>` 标签解析）、capabilities={function_calling: xml-relay, reasoning: think-tag}
  - `siliconflow`：base_url=`https://api.siliconflow.cn/v1`、模型=`Qwen/Qwen3-Omni-30B-A3B-Thinking`（agent=多模态同模型）、key=`OPENAI_API_KEY`、media=`audio_url`+`video_url(max_frames=16, fps=1)`、thinking=不传参（-Thinking 恒思考）、capabilities={function_calling: native, reasoning: reasoning_content}
  - `generic`：全部显式填写；media/thinking 映射默认按 vllm 风格（保守，假设 vllm-omni 类端点）
- 覆盖优先级：显式环境变量 > preset 默认

### 2. Relay 分流（agent 通道，sse_relay.py + main.py）

- 前端模型名改占位符，由 relay 服务端注入 `LLM_MODEL`
- `vllm` 分支：现有逻辑**原样保留**（剥 tools/stream_options、强制 stream、`<tool_call>` XML→tool_calls delta、`<think>` 文本透传）
- `siliconflow` 分支：tools 原样转发、上游 SSE 逐条透传（含 `reasoning_content`/`tool_calls` delta/`finish_reason`）、模型名注入
- 前端 reasoning 双通道并存：`extractReasoningMiddleware({tagName:"think"})` 保留（vllm 路径用，siliconflow 路径自然无 `<think>` 标签为空操作）；AI SDK 原生解析 `reasoning_content`（siliconflow 路径用）。前端零 provider 感知

### 3. 多模态适配层（summarizer.py + 新模块）

- 新 provider 模块：summarizer 构造 provider 无关的中性 parts（音频/视频/帧 + 语义参数），适配层按 preset 转 wire format
- `vllm`：`input_audio` + `video_url`（现状逐字节等价——回归红线）
- `siliconflow`：`audio_url`（mp3 base64）+ `video_url`（data URI + `max_frames=16`/`fps=1`）+ `image_url`（不变，OpenAI 标准）
- `_ThinkStripper` 增加 `reasoning_content` 通道（推理流中 → `pg.stage="thinking"`；正文首 token → `summary`）；vllm 路径不变化
- 收敛 3 处内联配置（summarizer.py:526/860、main.py:565）为共享客户端工厂
- 短视频 <90s 阈值不变；硅流 ≤30s 建议 → 实测观察 30–90s 表现，必要时另立小任务调阈值

### 4. 前端（route.ts）

- 模型名占位符化；其余零变化（`FASTAPI_URL` 不变）

### 5. Docker 主逻辑镜像（新 Dockerfile + 入口脚本）

- 单镜像：FastAPI（:8000）+ Next.js 生产构建（:3000），轻量入口脚本编排
- 内容：Python 3.11 + uv、ffmpeg、MediaCrawler checkout + playwright 依赖、faster-whisper（完整依赖，Q16=A）
- 运行推荐 `--network=host`（CDP :9222 复用宿主机转发，解决容器内无法 localhost 访问 Windows Chrome 的问题）
- 两种拓扑：
  - 场景一（配置足够）：宿主跑 vLLM 部署脚本 + 镜像主逻辑指 localhost
  - 场景二（配置不足）：仅镜像 + `LLM_PROVIDER=siliconflow|generic` + `OPENAI_API_KEY`
- 初版交付 Dockerfile + 构建说明（不推送镜像仓库，发布阶段再定）

### 6. vLLM-omni 独立部署脚本（scripts/deploy_vllm_omni.sh + 文档）

- 环境检查（nvidia-smi，<24GB 提示）→ 依赖安装（vllm-omni ≥0.18.0）→ 模型下载（`MODEL_SOURCE=hf|modelscope` 环境变量切换）→ 启动（`--omni` 标志，配方对齐现有 start_vllm_bare.sh）
- Linux 原生与 Windows-WSL2 两条文档路径；默认 AWQ 4bit 变体（与现役配方一致）
- 验证：本 session 在现有 AutoDL 实例做无损片段验证（命令正确性）；发布前检查清单新增「全新 24GB 机器完整跑通」

### 7. 文档交付物

- `.env.example`：provider 注释 + siliconflow 示例（注释态）
- `README`：配置章节（双拓扑：本地模型 / 远程 API）+ Docker 使用 + 部署脚本引用
- `CONTEXT.md`：修正「多模态总结」术语（补短视频 video_url 路径）；新增「模型提供方」「Provider 预设」术语；「部署拓扑」节补本地部署/退化方案
- `docs/adr/0005-multi-provider-model-access.md`：多提供方接入与部署拓扑决策（符合 ADR 三条件：难逆转、无上下文难理解、真实权衡）
- 发布前检查清单新增：场景一（本地模型+docker 主逻辑）与场景二（远程 API+docker）新环境完整验证、vLLM 脚本全新机器验证

## 实测协议

① **HTTP 冒烟**（curl 直连 SiliconFlow，key 从 `/tmp/siliconflow.key` 读取，不经过本地服务器）：
  - 纯文本流式 + `reasoning_content` 验证
  - `audio_url`（mp3 base64）验证
  - `video_url` + `max_frames/fps` 验证
  - 原生 function calling 小样本（5–10 次，tools+tool_choice，含 `modalities=["text"]` 有无对照）
② 代码改造（按上文）
③ SiliconFlow 端到端（裸机）：agent 对话含工具调用全链路（搜索→下载→总结）、短视频总结、长视频总结
④ **vLLM 回归（红线）**：切回 vllm preset，全链路重测（agent + 短/长视频总结）
⑤ 镜像构建 + 镜像内场景二冒烟（对话含工具调用一次 + 短视频总结一次）
⑥ vLLM 部署脚本无损片段验证（AutoDL 现有实例）

## 实施验证记录（2026-08-13 晚）

- ①②③④⑥ 全部完成并通过（commit c205236 + 915666d）；⑤ 镜像构建 + 场景二冒烟完成（2.83GB，commit 见后续）
- **Docker 构建环境要点**（WSL2 + clash）：构建必须 `--network=host` + `--build-arg HTTP_PROXY=http://127.0.0.1:7890`（容器内 127.0.0.1 不通宿主 clash，普通 build-arg 代理只对拉镜像生效）；`.dockerignore` 不支持行内注释；hatchling 构建包需要 README.md 出现在 COPY 层
- **运行**：`--network=host` 在宿主 3000/8000 被占时不可用 → 桥接 + `-p` 映射即可（容器访问第三方 API 走 NAT 直连正常）
- **镜像不含 TransNetV2**（需 torch 体积过大）：场景检测自动回退 ffmpeg，Phase 2 已禁用故功能无损
- **SiliconFlow 平台现象**：多模态端点间歇性 401 "Token is invalid"（新 key 鉴权缓存不稳定，纯文本端点不受影响），自愈周期约 1-5 分钟；期间流式请求会 ReadTimeout 中断。**非我方代码问题**（裸机同 payload 复现）；生产可考虑在 summarizer 对 401 加重试
- 冒烟结论：容器内 agent 工具调用（finish=tool_calls + 参数正确）+ 多模态总结（1978 字结构化）全绿

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| SiliconFlow Omni 原生 function calling 不稳 | ① 冒烟先行小样本验证；不稳则 relay 硅流分支降级为 XML 模式（改动局限于 relay 一处） |
| 官方文档对 Omni 思考参数矛盾 | -Thinking 变体恒思考，不传参规避 |
| 视频 ≤30s 建议 vs 现有 <90s 阈值 | 实测观察 30–90s 表现，必要时另立小任务 |
| 模型状态页显示 "Deprecated" | 实测确认可购可用（冒烟即证） |
| 网络可达性（WSL + clash 代理环境） | 冒烟时测直连/代理两态，curl 需注意 `--noproxy` |

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/vidagent/config.py` | provider preset 系统（LLM_PROVIDER 新值 + preset dataclass） |
| `src/vidagent/llm_provider.py`（新） | 共享客户端工厂 + media wire format 适配 |
| `src/vidagent/tools/summarizer.py` | 中性 parts 构造 + `_ThinkStripper` reasoning_content 通道 + 内联配置收敛 |
| `server/main.py` | relay 分流入口 + 批量总结配置获取收敛 |
| `server/sse_relay.py` | siliconflow 透传模式（tools 转发 + reasoning_content/tool_calls SSE 透传） |
| `frontend/src/app/api/chat/route.ts` | 模型名占位符化 |
| `Dockerfile`（新）+ `docker/entrypoint.sh`（新） | 主逻辑单镜像 |
| `scripts/deploy_vllm_omni.sh`（新） | vLLM-omni 独立部署脚本 |
| `.env.example` | provider 注释 + siliconflow 示例 |
| `README.md` | 配置/部署章节 |
| `CONTEXT.md` | 术语修正与新增 |
| `docs/adr/0005-multi-provider-model-access.md`（新） | ADR |
