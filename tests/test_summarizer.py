from pathlib import Path

from vidagent.tools import summarizer


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
