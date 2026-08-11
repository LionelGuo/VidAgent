# vLLM-Omni 启动报错 `AssertionError: duplicate template name` 排查与修复

## 概述

在 AutoDL 实例克隆后，使用原命令启动 vLLM-Omni 模型服务时，抛出 `AssertionError: duplicate template name` 错误，服务无法启动。本文档记录根因分析、修复步骤及预防措施。

- **日期**：2026-08-11
- **服务器**：AutoDL (westd.seetacloud.com)，SSH 端口 47696
- **模型**：Qwen3-Omni-Thinking-AWQ-4bit (`/root/autodl-tmp/Qwen3-Omni-Thinking-AWQ-4bit`)
- **vLLM 版本**：vllm-omni 0.26.0
- **PyTorch 版本**：2.11.0+cu130 (混杂了 2.7 时代的 173 个孤儿文件)

## 报错命令

```bash
vllm-omni serve /root/autodl-tmp/Qwen3-Omni-Thinking-AWQ-4bit \
  --port 6006 \
  --gpu-memory-utilization 0.85 \
  --max-num-batched-tokens 49152 \
  --max-num-seqs 2 \
  --enable-prefix-caching
```

## 根因分析

### 一句话总结

**与 chat template / tokenizer 毫无关系。** 真正原因是 PyTorch 安装目录中存在 173 个旧版本（torch 2.7, 2024-11-25 mtime）的孤儿 `.py` 文件，导致 `torch._inductor.kernel` 子模块被重复导入，触发 `TritonTemplate` 类级字典的重复注册断言。

### 详细失败链

```
1. vllm-omni serve
   → import vllm
   → vllm/env_override.py
   → import torch._inductor.lowering

2. torch 的模块导入机制遍历 torch/_inductor/kernel/ 下所有 .py 文件
   → 逐个执行模块级代码

3. 该目录中存在 4 个 torch 2.7 时代的孤儿文件：
   - flex_attention.py
   - flex_decoding.py
   - mm_scaled.py
   - unpack_mixed_mm.py

4. 第一轮导入：
   - flex_attention.py 先执行，在 TritonTemplate.all_templates（类级字典）
     注册了 "flex_attention"
   - mm_scaled.py 因 ImportError 导入失败
     （新版 torch 2.11.0 的 mm_common.py 已移除 scaled_mm_configs）
   - 失败后 sys.modules 中部分已加载模块被清除

5. 第二轮导入（重试机制）：
   - flex_attention.py 模块级代码再次执行
   - 尝试在 TritonTemplate.all_templates 中再次注册 "flex_attention"
   - select_algorithm.py:1775 → assert name not in self.all_templates
   - 触发 AssertionError: duplicate template name
```

### 为什么会"混装"

```
| 文件来源    | mtime 范围   | 文件数 | 备注                       |
|------------|-------------|--------|----------------------------|
| torch 2.7  | 2024-11-25  | 173    | 孤儿文件，不在 2.11.0 RECORD |
| torch 2.11 | 2026-08-07  | 其余    | 正常安装                    |
```

**混装原因**：
1. torch 从 2.7 升级到 2.11.0 时，旧版本的 `RECORD` 文件已不存在
2. pip 依赖 `RECORD` 来识别哪些文件属于当前包——旧 RECORD 缺失 → pip 无法完整卸载旧版本
3. 升级时新文件写入 `site-packages/torch/`，但同名旧文件被覆盖、**不同名的旧文件则残留**
4. 实例克隆（数据盘+系统盘原样拷贝）将这种损坏状态原样保留

## 修复步骤

### 1. 识别并删除孤儿文件

```bash
SP=/root/miniconda3/lib/python3.12/site-packages
RECORD=$SP/torch-2.11.0.dist-info/RECORD

# 找出 torch 目录下 mtime 在 2024-11-25 前后的文件（torch 2.7 时代）
# 交叉验证：确认这些文件均不在 torch 2.11.0 RECORD 中
find $SP/torch -newermt '2024-11-24' ! -newermt '2024-11-26' \
  -type f ! -path '*__pycache__*' | \
  while read f; do
    rel=${f#$SP/}
    grep -qF "$rel" "$RECORD" || echo "$f"
  done > /tmp/torch_orphans.txt

# 输出 173 个孤儿文件——全部不在 2.11.0 RECORD 中
# 安全删除
while read f; do
  rm -f "$f"
  rm -f "$(dirname "$f")/__pycache__/$(basename "$f")"*.pyc
done < /tmp/torch_orphans.txt
```

### 2. 清理 vLLM 编译缓存

```bash
rm -rf /root/.cache/vllm
```

> vLLM 的编译缓存（`torch_compile_cache`）可能包含旧版本 torch 的编译产物，不清除可能导致后续启动时的其他错误。

### 3. 验证导入

```bash
/root/miniconda3/bin/python -c "
import torch
import torch._inductor
import vllm
import vllm_omni
print('ALL OK')
"
```

输出 `ALL OK` 且无任何错误 → 修复成功。

### 4. 重新启动服务

```bash
nohup /root/miniconda3/bin/vllm-omni serve \
  /root/autodl-tmp/Qwen3-Omni-Thinking-AWQ-4bit \
  --port 6006 \
  --gpu-memory-utilization 0.85 \
  --max-num-batched-tokens 49152 \
  --max-num-seqs 2 \
  --enable-prefix-caching \
  > /tmp/vllm_startup.log 2>&1 &
```

首次加载约 5 分钟（克隆盘 XFS 读盘较慢，~40s/shard），后续启动会更快。

### 5. 验证服务正常

```bash
# 检查服务是否在监听
curl -s http://localhost:6006/v1/models | head -20

# 检查 GPU 显存占用
nvidia-smi | grep vllm
```

## 验证结果

| 项目 | 状态 |
|------|------|
| torch 2.11.0+cu130 导入 | ✅ |
| vllm 0.26.0 / vllm-omni 0.26.0 导入 | ✅ |
| 模型加载 | ✅ 完成 |
| GPU 显存 | 39963 MiB / 49152 MiB |
| Port 6006 监听 | ✅ |
| `/v1/models` 接口 | ✅ 正常返回 |
| 对话 smoke test | ✅ 正常输出（含 `<think>` 标签） |

## 实例迁移检查清单

当克隆/迁移 AutoDL 实例时，建议按以下步骤检查环境：

```bash
# 1. 验证 pip 包完整性
pip check

# 2. 检查 torch 是否有孤儿文件
SP=$(python -c "import site; print(site.getsitepackages()[0])")
RECORD=$SP/torch-*.dist-info/RECORD
# 用 find + mtime 检查异常文件（见上文"修复步骤 1"）

# 3. 清理编译缓存
rm -rf ~/.cache/vllm/  ~/.cache/torch/

# 4. 验证关键导入
python -c "import torch, vllm; print(torch.__version__)"
```

## 预防措施

1. **升级 torch/vllm 时**：先 `pip uninstall` 再 `pip install`，而非直接 `pip install --upgrade`
2. **或**：升级后运行以下清理脚本，对比 RECORD 删除孤儿文件
3. **实例克隆后**：运行 `pip check` + 关键导入验证
4. **可选**：在 Docker 中运行模型服务，避免系统级包管理问题

## 当前实例信息

| 项目 | 值 |
|------|-----|
| SSH | `ssh -p 47696 root@connect.westd.seetacloud.com` |
| API Endpoint | `https://u805822-f4uf-9f7487a8.westd.seetacloud.com:8443/v1` |
| 模型路径 | `/root/autodl-tmp/Qwen3-Omni-Thinking-AWQ-4bit` |
| 服务端口 | 6006 (内网), 8443 (公网端口映射) |
| 启动脚本 | `scripts/start_vllm_bare.sh` |

## 参考资料

- vLLM 源码：`select_algorithm.py:1775` — `TritonTemplate.all_templates` 重复注册断言
- torch 模块导入：`torch._inductor.lowering` → `import_submodule` 遍历目录加载
- pip RECORD 机制：`site-packages/<package>.dist-info/RECORD` 记录包的所有文件
