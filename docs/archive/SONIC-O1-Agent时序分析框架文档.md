
# 视频总结 Agent：VidAtlas + SONIC-O1 融合架构工程参考文档

## 1. 宏观系统架构 (System Architecture)

系统采用 **“前端主导调度 (Frontend-Driven Orchestration)”** 模式。Vercel AI SDK 作为整个 Multi-Agent 逻辑的大脑，通过 `maxSteps` 特性自动维持“观察 -> 思考 -> 调用工具 -> 再思考”的循环。

*   **Vercel AI SDK (React/Next.js)**：负责维持对话生命周期、解析后端返回的 `<tool_call>` XML、渲染 Generative UI。
*   **FastAPI (Tool Execution & Relay)**：作为中间件层，承接视频下载、FFmpeg 抽帧预处理任务，并将标准的 OpenAI Tool Call 格式转换为 vLLM 可理解的 XML/Prompt，再将 vLLM 的流式输出转义回 SSE 给前端。
*   **vLLM (Qwen3-Omni-30B)**：作为纯粹的无状态推理引擎，负责理解长音频、离散帧和文本提示。

---

## 2. 核心工作流设计 (Core Workflow: The 3-Step Loop)

利用 Vercel AI SDK 的连续工具调用能力，整个总结过程被拆分为三个自动流转的阶段：

### 阶段一：前置感知与细粒度锚点生成 (VidAtlas Pass 1)
*   **触发条件**：用户输入视频 URL，AI SDK 将请求发往 FastAPI，模型决定调用工具 `preprocess_video(url)`。
*   **FastAPI 后端执行逻辑 (ThreadPoolExecutor)**：
    1.  **音频分离**：提取完整音频流（转为 Base64 或保存至高可用对象存储，返回 URI）。
    2.  **物理边界计算 (Hybrid Boundary Detection)**：
        *   运行 `FFmpeg Scene Detect` 提取画面突变点。
        *   运行 `Audio Silence Detection` 提取语音停顿点。
        *   执行合并逻辑：取两者的并集，并应用约束（例如：两个切点间距 `< 30s` 则合并；若 `> 120s` 无切点则强制在 `90s` 处增加兜底切点）。
    3.  **稀疏抽帧**：仅在上述计算出的“候选边界点”进行单帧截图。
*   **Tool Result 回传**：FastAPI 将【完整音频 URI】+【候选边界时间戳列表】+【对应边界的稀疏帧 URI】返回给 Vercel AI SDK。

### 阶段二：全局规划与粗粒度聚合 (VidAtlas Pass 2 + SONIC-O1 Planner)
*   **触发条件**：Vercel AI SDK 收到 `preprocess_video` 的结果，自动带着这些结果向 vLLM 发起下一轮推理 (Step 2 of `maxSteps`)。
*   **模型内部推理逻辑**：
    1.  模型接收完整音频和稀疏的候选帧。
    2.  利用 System Prompt 强制约束：模型划分的章节边界，**只能从工具返回的候选边界时间戳中选取**，确保时间戳零幻觉。
    3.  模型输出全局章节大纲。如果模型认为某一段（如 `[03:20] - [04:50]`）具有重要的视觉动作，但单张稀疏帧无法判断，则在内部推理中触发“需要验证”的逻辑。
*   **输出动作**：模型决定调用工具 `zoom_in_verify(start_time, end_time, global_context)`。

### 阶段三：局部验证与缝合 (SONIC-O1 Verification)
*   **触发条件**：Vercel AI SDK 解析到 `zoom_in_verify` 的工具调用。
*   **FastAPI 后端执行逻辑**：
    1.  根据传入的 `start_time` 和 `end_time`，对该局部片段执行**高密度抽帧**（如 1 fps）。
    2.  将这批密集帧列表返回给前端 SDK。
*   **终局推理**：模型 (Step 3 of `maxSteps`) 结合之前建立的 `global_context` 和刚刚拿到的高密度连续帧，补全缺失的视觉细节。最终输出包含精确时间戳和详尽总结的 JSON 或文本。

---

## 3. 关键工具接口契约设计 (Tool Schema Design)

为了让 Vercel AI SDK 能够无缝编排，以下工具的 Schema 定义至关重要（以 Zod 格式描述逻辑）：

### Tool 1: `preprocess_video`
*   **输入参数**：
    *   `video_url` (String): 目标视频链接。
*   **输出响应 (返回给大模型的内容)**：
    *   `audio_stream_uri`: 完整音频的访问路径。
    *   `candidate_boundaries`: 数组格式，如 `[0, 45, 120, 190, 245]` (单位: 秒)。
    *   `sparse_frames`: 与边界对应的图片 URI/Base64 列表。

### Tool 2: `zoom_in_verify`
*   **输入参数**：
    *   `start_time` (Number): 需放大的起始时间（基于 Tool 1 提供的候选边界）。
    *   `end_time` (Number): 需放大的结束时间。
    *   `global_context` (String): **核心**。模型在调用此工具时，必须强制传入它目前理解的上下文（例如：“目前正在评测两款手机的铰链，需要确认具体手势”），防止局部验证时出现上下文断裂。
    *   `verification_query` (String): 模型需要确认的具体视觉问题（例如：“主角按下了哪个颜色的按钮？”）。
*   **输出响应**：
    *   `dense_frames`: 高密度图像帧列表。

### Tool 3: `generate_final_report` (可选，用于严格结构化输出)
*   **作用**：为了方便前端渲染复杂的 Generative UI (如交互式思维导图)，可要求模型在最后一步强制调用此工具来输出标准化的 JSON 结构，而非散装文本。

---

## 4. 技术挑战与架构防御策略 (Engineering Mitigations)

针对你现有的自研 FastAPI + Vercel AI SDK 栈，团队在实现时需重点关注以下问题：

### A. Vercel AI SDK / 浏览器超时问题 (Timeout Mitigation)
*   **风险**：`preprocess_video` 包含视频下载和 FFmpeg 处理，极易触发传统 HTTP 请求的 30s/60s 超时限制。
*   **解决方案**：
    *   **不要做同步 Tool Call**。FastAPI 中的工具接口应采用异步 Task 模式。
    *   前端 SDK 发起工具调用后，FastAPI 立即返回一个带有 `task_id` 和 `status: "processing"` 的伪结果给大模型。
    *   同时，FastAPI 通过 SSE 向前端推送自定义数据 (`ai SDK data stream`) 更新 UI 进度。
    *   **架构变形**：考虑到 Vercel AI SDK 连续工具调用的局限性，建议 `preprocess_video` 阶段在前端通过独立 API 异步完成，等拿到了物理边界和稀疏帧后，再将其作为 `initialMessages` 喂给 Vercel AI SDK 启动大模型的推理循环。

### B. vLLM KV Cache 管理与显存控制
*   **风险**：在 SONIC-O1 逻辑中，模型要多次参与多轮对话（全局规划 -> 局部验证 -> 总结），长音频和大量图像的 Context 会导致 KV Cache 暴涨。
*   **解决方案**：
    *   **Prompt 缓存 (Automatic Prompt Caching, APC)**：确保 vLLM 开启了 Prefix Caching。将“长音频 Base64”放置在 Prompt 的最前端（System Prompt 之后），并在整个 `maxSteps` 循环中保持其位置绝对固定。这样 vLLM 只会在第一轮计算音频的 KV Cache，后续的局部验证轮次可直接复用，极大提升吞吐。
    *   **Token 卸载**：如果 `zoom_in_verify` 抽取了 20 张局部高密帧，在验证结束后，将这些高密帧的记录从 Vercel AI SDK 的 `messages` 历史中“截断或替换”为纯文本结论，再发给大模型进行下一步，防止 Context Window 超载。

### C. SSE Relay (XML 转换层) 的稳定性
*   **要求**：vLLM 的 Qwen3-Omni 通常输出类似于 `<tool_call>{"name": "zoom_in", "args": {...}}</tool_call>` 的格式。
*   **设计**：你的 FastAPI Relay 层在做 Stream 转换时，需要实现可靠的 XML 缓冲区（Buffer）解析。只有完整提取出 `<tool_call>` 的内部 JSON，才能将其映射为符合 Vercel AI SDK 期望的 `{"type": "function", "function": {"name": "...", "arguments": "..."}}` Delta 对象，确保前端的 `toolInvocations` 状态能被正确触发。

---

## 5. UI/UX 映射关系 (Generative UI Alignment)

利用 Next.js 15 和 React 19 的新特性，后端架构能直接驱动前端体验：
*   当前端拦截到 `preprocess_video` 调用时，UI 渲染 `<SkeletonVideoPlayer />` 和加载动画。
*   当模型输出全局章节阶段，利用 React 的流式渲染，渐进式地展示带有精确边界的 `<ChapterList />`。
*   当前端拦截到 `zoom_in_verify` 时，UI 显示局部微交互动画：“*Agent 正在放大 03:20 的细节画面...*”。
*   当整个流程结束（`maxSteps` 完成），将最终结果映射到 Vidstack 播放器的时间轴 Marker 和 React Flow 思维导图中。
