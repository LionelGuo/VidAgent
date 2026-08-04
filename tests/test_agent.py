def test_build_agent_wires_three_tools():
    """Agent 应注册三大工具（构建过程不触网）。"""
    from vidagent.agent import build_agent

    agent = build_agent()
    assert len(agent.tools) == 3
