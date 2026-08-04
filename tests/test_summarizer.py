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
