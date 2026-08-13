# 多平台模型 API 接入（vLLM-omni / SiliconFlow / 通用 OpenAI 兼容端点）

开源分发要求：用户不都使用自托管 vLLM-omni，需支持配置其他平台 API key。
我们决定引入 **provider 预设系统**（`src/vidagent/llm_provider.py`）承载三平台差异——
relay 模式（XML 手写协议 vs 原生透传）、多模态 wire format（input_audio vs audio_url）、
推理解析模式（`<think>` 标签 vs `reasoning_content` 字段）；agent 与多模态端点可分离配置，
单端点平台（SiliconFlow）开箱即用。终态目标是统一 OpenAI API，待 vllm-omni 上游支持原生
function calling 后删除 XML 转换分支收敛——预设系统是迈向终态的过渡，而非永久多路。

## 考虑的方案

- **relay 分流（采纳）**：前端单代码路径（占位符模型名），relay 按 provider 分支。vllm 走现有
  XML 转换逻辑原样保留；siliconflow/generic 走原生透传（tools 转发、tool_choice 规范化为 auto、
  SSE 逐行透传含 reasoning_content/tool_calls）。
- 前端直连第三方平台（拒绝）：两条前端路径，测试面翻倍，前端被迫感知 provider。
- 第三方平台也走 XML 手写协议（拒绝）：把 bare mode 的妥协传染给标准平台，且依赖模型遵守手写约定。

## 关键约束

- **红线**：vLLM 路径逐字节等价——XML relay、input_audio wire format、`<think>` 解析均不变。
  relay 现在注入 model（来自 `LLM_MODEL`），其值与原前端硬编码一致，故 wire 字节不变。
- **SiliconFlow 仅支持 `tool_choice=auto`**（`required` / 指定具体函数均 400）——relay 透传分支强制规范化。
- **-Thinking 变体恒思考**，无需传 `enable_thinking`——规避官方文档对该参数是否覆盖 Omni 的矛盾。

## 后果

- relay 成为 provider 差异的唯一收敛点；新增 OpenAI 兼容平台只需加一个 preset + 可能的 media 映射。
- 前端 `<think>` middleware（vllm 路径）与 AI SDK 原生 reasoning_content（siliconflow 路径）两条推理通道并存，
  互不干扰（siliconflow 路径无 `<think>` 标签，middleware 为空操作）。
- vllm-omni 原生 function calling 成熟后，删除 relay 的 XML 分支即可收敛到统一透传——预设系统届时退化为纯透传。
