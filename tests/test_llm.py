from vidagent.config import settings
from vidagent.llm import build_model


def test_build_model_uses_active_llm():
    m = build_model()
    _, _, model = settings.active_llm()
    assert m.id == model


def test_system_role_not_developer():
    """DeepSeek/Ollama 不认 developer 角色，必须映射回 system。"""
    m = build_model()
    assert m.role_map["system"] == "system"
