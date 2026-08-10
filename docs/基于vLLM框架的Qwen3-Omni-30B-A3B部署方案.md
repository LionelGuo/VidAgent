# 📄 工程实施方案：Qwen3-Omni-30B-A3B 全模态推理服务部署

## 1. 架构设计与核心策略
针对单卡 48GB VRAM 的物理限制，本方案摒弃 `transformers + bitsandbytes` 的“先加载FP16再动态量化”方案，全面转向 **“预量化 (AWQ) + vLLM 连续批处理”** 的工业级部署架构。

*   **计算硬件**：单卡 48GB VRAM (如 RTX 3090/A6000/L40S 等虚拟或物理实例)
*   **推理框架**：vLLM (专为高并发、高显存利用率设计)
*   **模型选型**：`Qwen3-Omni-30B-A3B-Instruct-AWQ` (INT4 预量化版本)
*   **输入/输出策略**：
    *   **输入**：支持图片、音频、视频序列的多模态联合输入。
    *   **输出**：通过标准 OpenAI Chat API 限制，仅调用文本生成端点，禁用音频生成解码器。

## 2. 部署前置准备 (环境与模型)

### 2.1 依赖环境安装
建议使用 Python 3.10+，并安装支持多模态分支的最新 vLLM：
```bash
# 升级并安装 vllm 及相关依赖
pip install --upgrade pip
pip install vllm accelerate autoawq
pip install openai # 客户端调用使用
```

### 2.2 模型获取 (打破 OOM 死局的关键)
**严禁**使用原版 FP16 权重进行部署，必须获取预量化版本。
*   **途径 1 (推荐)**：在 Hugging Face 或 ModelScope 直接下载已打包好的 AWQ/GPTQ 预量化权重库（例如：`Qwen/Qwen3-Omni-30B-A3B-Instruct-AWQ`）。权重体积应在 **18GB - 20GB** 之间。
*   **途径 2 (自制)**：若无现成开源版本，请在无 GPU 的大内存 CPU 机器上（RAM > 128GB），使用 `AutoAWQ` 进行离线量化并保存，然后将这 20GB 文件拷贝至 48GB GPU 机器。

## 3. 服务端部署配置 (vLLM)

在获取到 AWQ 预量化模型后，通过以下命令拉起推理后端。

### 3.1 启动脚本 (`start_server.sh`)
```bash
#!/bin/bash

# 假设模型存放在本地的 /data/models/Qwen3-Omni-30B-AWQ
MODEL_PATH="/data/models/Qwen3-Omni-30B-AWQ"

python3 -m vllm.entrypoints.openai.api_server \
    --model $MODEL_PATH \
    --quantization awq \
    --trust-remote-code \
    --omni \
    --gpu-memory-utilization 0.95 \
    --max-model-len 16384 \
    --max-num-seqs 16 \
    --limit-mm-per-prompt '{"audio": 1, "video": 1, "image": 8}' \
    --port 8000
```

### 3.2 核心参数工程解析（针对你们的需求）：
1.  **`--quantization awq`**：告知 vLLM 直接将文件作为 4-bit 映射进显存，**彻底消除 >70GB 的加载峰值，完美适配 48GB 显卡**。
2.  **`--gpu-memory-utilization 0.95`**：榨干硬件。模型占用约 20GB，剩下的 ~25GB 全部划拨给 vLLM 的 KV Cache 池，用于支撑音视频极长的 Token。
3.  **`--max-model-len 16384`**：音视频特征提取后 Context 非常长，设为 16k 甚至 32k 可防止长视频直接溢出报错。
4.  **`--max-num-seqs 16`**：由于你们**预期最高并发 10 路**，限制最大并发序列数为 16，防止当 10 个请求同时传入 1 分钟大视频时，瞬间把 KV Cache 打满导致后端崩溃。
5.  **`--limit-mm-per-prompt`**：这是多模态并发的生命线。显式规划显存布局，允许每个请求最多带 1 个音频、1 个视频，如果不加此参数多模态服务将无法安全启动。

## 4. 客户端开发接入 (限制仅输出文本)

虽然 Qwen3-Omni 具备吐出音频的能力，但只要客户端**走标准的 `/v1/chat/completions` 接口，并在请求时不要求生成 `modalities=["audio"]`**，模型就会安分守己地只输出文本。

### Python 客户端调用示例
开发团队可以直接使用官方的 `openai` 库，把 base_url 换成本机 vLLM 地址：

```python
from openai import OpenAI
import base64

# 初始化本地 vLLM 客户端
client = OpenAI(
    api_key="EMPTY",
    base_url="http://localhost:8000/v1",
)

def encode_file_to_base64(file_path):
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

# 将待处理的音视频转为 base64
audio_base64 = encode_file_to_base64("sample.wav")
video_base64 = encode_file_to_base64("sample.mp4")

# 构造请求：只请求文本输出
response = client.chat.completions.create(
    model="/data/models/Qwen3-Omni-30B-AWQ",
    messages=[
        {
            "role": "system", 
            "content": "你是一个严谨的多模态分析助手，请仔细分析用户提供的视频和音频，并仅以文字形式输出分析报告。"
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请结合视频画面和这段音频，提取其中的核心事件和关键对话。"},
                {
                    "type": "input_audio", 
                    "input_audio": {"data": audio_base64, "format": "wav"}
                },
                # vLLM 支持多模态协议，具体字段以当时 vLLM 文档为准（通常通过 image_url 传入多帧，或 video_url）
                {
                    "type": "video_url",
                    "video_url": {"url": f"data:video/mp4;base64,{video_base64}"}
                }
            ],
        }
    ],
    temperature=0.2, # 低温可保证输出的严谨性，减少胡说八道
    stream=True # 推荐使用流式输出文本，提升用户体验
)

# 纯文本流式输出
for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## 5. 显存与并发性能测算 (团队监控参考)

根据本方案部署，显卡（48GB）的实时状态预估如下：

| 阶段 | 显存占用 | 说明 |
| :--- | :--- | :--- |
| **冷启动** | 0 GB -> ~20 GB | 仅将 AWQ 权重直接 Load 到显存，安全平滑。 |
| **vLLM 预热完毕** | ~45.6 GB | vLLM 会瞬间圈占 `48G * 0.95 = 45.6G` 的显存。其中约 25G 变为 KV Cache 池。**这是正常现象，并非泄漏！** |
| **运行时 (1路视频请求)** | ~45.6 GB | 请求到来时，vLLM 从那 25G 的预留池里动态分配 Block。视频通常耗费较多 Token，可能瞬间用掉 2-3 GB Cache。 |
| **运行时 (10路并发)** | ~45.6 GB | 10路任务同时调度，消耗约 20GB+ Cache。由于系统预留充足，刚好可以安全消化，不会 OOM。 |

## 6. 避坑与运维监控建议

1. **“假 OOM” 与真排队**：
   开发团队在发起超 10 个并发请求时，vLLM 可能不会报错 OOM，而是会让第 11 个请求在队列里等待（因为 KV Cache 已经被前 10 个占满了）。这是 vLLM 保护机制。可以通过 `curl http://localhost:8000/metrics` 监控 vLLM 的 `vllm:num_requests_waiting` 指标。
2. **视频预处理（极大降低显存压力）**：
   强烈建议在前端或中间件将视频进行**抽帧处理**（如 1 FPS）并压缩分辨率后再传给 API。原生 4K 60帧视频丢给模型不仅没有收益，还会让 Token 长度暴增，导致并发量从 10 跌至 2 或 3。
3. **禁用录音与语音合成开销**：
   因为你们彻底不需要语音输出，这直接免去了 Talker/TTS 模块（~3B 参数）的反向生成和巨大的显存开销，这使得单卡 48G 在应对 10路高并发时更加游刃有余。
