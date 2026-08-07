from pathlib import Path

from vidagent.tools import summarizer


def test_resolve_device_auto():
    assert summarizer._resolve_device() in ("cpu", "cuda")


def test_degradation_on_asr_failure(monkeypatch):
    """抽音失败时应降级：_summarize 收到空 transcript，且不抛异常。"""
    monkeypatch.setattr(summarizer.settings, "llm_multimodal", False)

    def boom(_path):
        raise RuntimeError("no audio track")

    monkeypatch.setattr(summarizer, "extract_audio", boom)

    captured = {}

    def fake_summarize(transcript, metadata):
        captured["transcript"] = transcript
        captured["metadata"] = metadata
        return "DEGRADED"

    monkeypatch.setattr(summarizer, "_summarize", fake_summarize)

    out = summarizer.extract_and_summarize("x.mp4", {"title": "T", "desc": "D"})
    assert out == "DEGRADED"
    assert captured["transcript"] == ""          # 降级：无转写
    assert captured["metadata"]["title"] == "T"  # 元数据仍传入


def test_transcript_cache_skips_asr(monkeypatch, tmp_path):
    """已有转写缓存时应直接读缓存，跳过抽音+ASR。"""
    monkeypatch.setattr(summarizer.settings, "llm_multimodal", False)

    cache = tmp_path / "BV1.transcript.txt"
    cache.write_text("缓存的转写文本", encoding="utf-8")
    monkeypatch.setattr(summarizer.storage, "transcript_path", lambda vid: cache)

    asr_called = {"v": False}

    def fake_transcribe(_):
        asr_called["v"] = True
        return ("", "")

    captured = {}

    def fake_summarize(transcript, metadata):
        captured["t"] = transcript
        return "SUMMARY"

    monkeypatch.setattr(summarizer, "_transcribe", fake_transcribe)
    monkeypatch.setattr(summarizer, "_summarize", fake_summarize)

    out = summarizer.extract_and_summarize("x.mp4", {"video_id": "BV1", "title": "T"})
    assert out == "SUMMARY"
    assert asr_called["v"] is False            # 未触发 ASR
    assert captured["t"] == "缓存的转写文本"    # 用了缓存文本


def test_live_asr_holder():
    """实时转写进度 holder：begin/update/reset 与查询函数。"""
    assert summarizer.live_active() is False
    assert summarizer.live_partial() == ""

    summarizer._live.begin()
    summarizer._live.update("部分转写文本")
    assert summarizer.live_active() is True
    assert summarizer.live_partial() == "部分转写文本"

    summarizer._live.reset()
    assert summarizer.live_active() is False
    assert summarizer.live_partial() == ""


def test_multimodal_path_with_flag(monkeypatch):
    """LLM_MULTIMODAL=true 时走多模态路径，不调 ASR 和文本总结。"""
    monkeypatch.setattr(summarizer.settings, "llm_multimodal", True)

    # mock 抽音：返回假 mp3
    monkeypatch.setattr(summarizer, "extract_audio", lambda _p: "/fake/audio.mp3")

    multimodal_called = {"v": False}

    def fake_multimodal(mp3, metadata, video_path=None):
        multimodal_called["v"] = True
        return "MULTIMODAL_SUMMARY"

    monkeypatch.setattr(summarizer, "_summarize_multimodal", fake_multimodal)

    asr_called = {"v": False}
    monkeypatch.setattr(
        summarizer, "_transcribe",
        lambda _p, **kw: asr_called.update({"v": True}) or ("", ""),
    )
    text_summary_called = {"v": False}
    monkeypatch.setattr(
        summarizer, "_summarize",
        lambda t, m: text_summary_called.update({"v": True}) or "TEXT",
    )

    out = summarizer.extract_and_summarize("x.mp4", {"title": "T"})
    assert out == "MULTIMODAL_SUMMARY"
    assert multimodal_called["v"] is True
    assert asr_called["v"] is False        # 未触发 ASR
    assert text_summary_called["v"] is False  # 未触发文本总结


def test_multimodal_fallback_on_failure(monkeypatch):
    """多模态失败时降级到元数据总结，不崩溃。"""
    monkeypatch.setattr(summarizer.settings, "llm_multimodal", True)
    monkeypatch.setattr(summarizer, "extract_audio", lambda _p: "/fake/audio.mp3")

    def boom(_mp3, _meta, video_path=None):
        raise RuntimeError("audio too large")

    monkeypatch.setattr(summarizer, "_summarize_multimodal", boom)

    degraded = {"v": False}

    def fake_summarize(transcript, metadata):
        degraded["v"] = True
        assert transcript == ""  # 降级：无转写
        return "DEGRADED"

    monkeypatch.setattr(summarizer, "_summarize", fake_summarize)

    out = summarizer.extract_and_summarize("x.mp4", {"title": "T"})
    assert out == "DEGRADED"
    assert degraded["v"] is True


def test_multimodal_passes_video_path(monkeypatch):
    """多模态路径将原始视频路径传入 _summarize_multimodal。"""
    monkeypatch.setattr(summarizer.settings, "llm_multimodal", True)
    monkeypatch.setattr(summarizer, "extract_audio", lambda _p: "/fake/audio.mp3")

    captured = {}

    def fake_multimodal(mp3, metadata, video_path=None):
        captured["mp3"] = mp3
        captured["video_path"] = video_path
        return "SUMMARY"

    monkeypatch.setattr(summarizer, "_summarize_multimodal", fake_multimodal)

    out = summarizer.extract_and_summarize("/videos/test.mp4", {"title": "T"})
    assert out == "SUMMARY"
    assert captured["mp3"] == Path("/fake/audio.mp3")
    assert captured["video_path"] == Path("/videos/test.mp4")
