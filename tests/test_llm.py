import pytest

import llm
from db import UserLLMConfig


@pytest.mark.asyncio
async def test_disallowed_provider_is_rejected():
    with pytest.raises(llm.ProviderNotAllowedError):
        await llm.generate_answer("q", [], UserLLMConfig("k" * 12, "evil-custom", "m"))


@pytest.mark.asyncio
async def test_empty_key_never_reaches_litellm():
    with pytest.raises(ValueError):
        await llm.generate_answer("q", [], UserLLMConfig("", "huggingface", "m"))


def test_config_repr_redacts_key():
    assert "user-key" not in repr(UserLLMConfig("user-key", "huggingface", "m"))