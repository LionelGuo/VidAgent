# ADR-0002: 视频分段精度改进方案调研

**日期**: 2026-08-10
**状态**: 已调研，待后续实施

## 背景

当前 `detect_boundaries()` 使用 ffmpeg scene detection (320px 缩略图, threshold=0.3) + Whisper VAD (`min_silence_duration_ms=2000`) 的混合方案。Whisper VAD 已替换原有的 ffmpeg silence detection，显著改善了语义停顿的检测。

但仍有局限：
- Scene detection 在 320px 下的误报/漏报
- VAD 只覆盖语音内容，纯音乐/环境音场景无效
- 合并阈值 (merge=10s) 产生过短段（如 13s）和过长段（如 114s）

## 已验证的方案

### Route A: Whisper VAD 替换 ffmpeg silence（✅ 已实施）

- `detect_boundaries()` 接受可选 `vad_boundaries` 参数
- `_do_boundaries()` 中用 faster-whisper (`base` 模型, CPU int8) 提取 VAD 段
- 参数：`min_silence_duration_ms=2000, min_speech_duration_ms=500, max_speech_duration_s=120`

### 问题：段长不均

7.8min 视频实际边界: `[0, 13, 34, 73, 165, 215, 274, 291, 351, 465]`
- 最短 13s（标题卡），最长 114s（近 2 分钟）
- 根因：scene detection 仍基于像素差，非语义

## 待实施的方案

### Route B: transnetv2-pytorch 替换 ffmpeg scene detection

- **原理**: 训练过的 CNN 专门做 shot boundary detection
- **精度**: F1 77.9 (ClipShots), 96.2 (BBC Planet Earth)
- **安装**: `pip install transnetv2-pytorch`
- **使用**: `model.detect_scenes("video.mp4")` → 返回场景时间戳列表
- **优势**: 远优于 ffmpeg 的像素差阈值法
- **风险**: 需要 GPU 才能实时；纯 CPU 上 ~0.5x 实时
- **组合**: VAD ∪ transnetv2 → 语义 + 视觉双通道

### Route C: 段长后处理

当前 merge=10s 后段长差异大。增加后处理步骤：
```python
MIN_SEG = 20  # 最短段长（秒）
# 合并短段到相邻段
# 拆分 >90s 的超长段（在中点处插入边界）
```

### Route D: 固定时长分段 + 内容感知合并

替代自适应边界检测：先均匀切分（如每 30s 一段），再用轻量 VLM 判断相邻段是否属于同一章节。类似 ISSA 管线的简化版。

## 参考资料

- transnetv2-pytorch: https://github.com/so Czech/transnetv2-pytorch
- Chapter-Llama (CVPR 2025): ASR transcript + frame captions → LLM chapters
- ISSA pipeline: Whisper + Moondream2 + Gemma-3-4B, 全本地
- Silero VAD: faster-whisper 内置，纯 CPU >10x 实时
- WFS-SB (arXiv 2603.00512): 小波变换语义边界检测，免训练
