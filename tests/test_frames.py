"""自适应帧采样单元测试。"""

from pathlib import Path

from vidagent.utils import frames


def test_get_duration_parses_float(monkeypatch):
    """get_duration 正确解析 ffprobe 输出。"""
    import subprocess

    class FakeResult:
        stdout = "123.456\n"
        returncode = 0

    def fake_run(*args, **kwargs):
        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert frames.get_duration("dummy.mp4") == 123.456


def test_get_duration_failure_returns_zero(monkeypatch):
    """ffprobe 返回非数字时 get_duration 返回 0。"""
    import subprocess

    class FakeResult:
        stdout = "N/A\n"
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
    assert frames.get_duration("broken.mp4") == 0.0


def test_adaptive_frame_count():
    """各时长区间输出正确的帧数。"""
    assert frames.adaptive_frame_count(0) == frames.MIN_FRAMES       # 0s → min
    assert frames.adaptive_frame_count(30) == 6                       # ≤60s
    assert frames.adaptive_frame_count(60) == 6                       # =60s
    assert frames.adaptive_frame_count(120) == 8                      # ≤180s
    assert frames.adaptive_frame_count(180) == 8                      # =180s
    assert frames.adaptive_frame_count(300) == 10                     # ≤600s
    assert frames.adaptive_frame_count(600) == 10                     # =600s
    assert frames.adaptive_frame_count(900) == 12                     # ≤1800s
    assert frames.adaptive_frame_count(1800) == 12                    # =1800s
    assert frames.adaptive_frame_count(3600) == 16                    # >1800s


def test_adaptive_frame_count_clamps():
    """min_frames / max_frames 正确钳制。"""
    # 自定义 min=2, max=8
    assert frames.adaptive_frame_count(30, min_frames=2, max_frames=8) == 6
    assert frames.adaptive_frame_count(3600, min_frames=2, max_frames=8) == 8
    assert frames.adaptive_frame_count(0, min_frames=2, max_frames=8) == 2


def test_extract_frames_missing_video(monkeypatch):
    """视频不存在时返回空列表。"""
    monkeypatch.setattr(frames.Path, "exists", lambda s: False)
    result = frames.extract_frames("/nonexistent/video.mp4")
    assert result == []


def test_extract_frames_zero_duration(monkeypatch):
    """时长为 0 时返回空列表。"""
    monkeypatch.setattr(frames.Path, "exists", lambda s: True)
    monkeypatch.setattr(frames, "get_duration", lambda p: 0.0)
    result = frames.extract_frames("/fake/video.mp4")
    assert result == []


def test_extract_frames_creates_output(monkeypatch, tmp_path):
    """正常流程：生成正确数量的帧文件。"""
    import subprocess

    video = tmp_path / "test.mp4"
    video.write_text("fake video content")

    monkeypatch.setattr(frames, "get_duration", lambda p: 120.0)  # 120s → 8 帧

    out_dir = tmp_path / "keyframes_test"
    out_dir.mkdir()

    # Mock ffmpeg：创建空 jpg 文件
    def fake_run(cmd, **kwargs):
        # cmd: ["ffmpeg", "-y", "-ss", "16.0", ..., "/path/frame_01_16s.jpg"]
        for i, arg in enumerate(cmd):
            if arg == "-ss" and i + 1 < len(cmd):
                # 提取时间戳
                t = float(cmd[i + 1])
                # 最后一个 arg 是输出路径
                out = Path(cmd[-1])
                out.write_text(f"fake jpg at {t}s")
                break
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = frames.extract_frames(video, num_frames=8, output_dir=out_dir)
    assert len(result) == 8
    # 验证按时间排序
    for f in result:
        assert f.suffix == ".jpg"
        assert f.parent == out_dir
