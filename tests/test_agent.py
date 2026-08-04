def test_build_agent_wires_three_tools():
    """Agent 应注册三大工具（构建过程不触网）。"""
    from vidagent.agent import build_agent

    agent = build_agent()
    assert len(agent.tools) == 3


def test_build_agent_has_multi_turn_memory():
    """Agent 应启用会话历史（db + add_history_to_context）以支持多轮对话。"""
    from vidagent.agent import build_agent

    agent = build_agent()
    assert getattr(agent, "add_history_to_context", False) is True
    assert getattr(agent, "db", None) is not None
    assert getattr(agent, "num_history_runs", 0) >= 1
