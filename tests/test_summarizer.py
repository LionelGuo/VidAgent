from vidagent.tools import summarizer


def test_resolve_device_auto():
    assert summarizer._resolve_device() in ("cpu", "cuda")


def test_degradation_on_asr_failure(monkeypatch):
    """抽音失败时应降级：_summarize 收到空 transcript，且不抛异常。"""
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
