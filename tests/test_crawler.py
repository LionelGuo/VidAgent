import pytest


@pytest.mark.asyncio
async def test_user_homepage_rejects_non_numeric_mid():
    """给 user_homepage 传昵称应被拦截（给出清晰提示，而不是触发 -352）。"""
    from vidagent.tools import crawler

    with pytest.raises(ValueError, match="数字 UID"):
        await crawler._bilibili("user_homepage", "老番茄", None, 5)
