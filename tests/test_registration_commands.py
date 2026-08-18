from unittest.mock import AsyncMock, patch

import pytest

import registration_commands as rc


def test_is_command_detection():
    assert rc.is_command("register huggingface m k")
    assert rc.is_command("REMOVE")
    assert rc.is_command("help")
    assert not rc.is_command("what is the refund policy?")
    assert not rc.is_command("")


@pytest.mark.asyncio
async def test_register_happy_path_upserts_own_number_only():
    with patch("registration_commands.db.upsert_user_llm_config", new=AsyncMock()) as up:
        reply = await rc.handle_command(
            "919876543210", "register huggingface org/model hf_secretkey123", is_group=False)
        up.assert_awaited_once_with("919876543210", "hf_secretkey123", "huggingface", "org/model")
        assert "Registered" in reply
        assert "hf_secretkey123" not in reply     # key never echoed


@pytest.mark.asyncio
async def test_register_in_group_refused_with_rotation_warning():
    with patch("registration_commands.db.upsert_user_llm_config", new=AsyncMock()) as up:
        reply = await rc.handle_command(
            "919876543210", "register huggingface m hf_secretkey123", is_group=True)
        up.assert_not_awaited()
        assert "Revoke" in reply


@pytest.mark.asyncio
async def test_register_bad_provider_and_bad_format():
    with patch("registration_commands.db.upsert_user_llm_config", new=AsyncMock()) as up:
        assert "not supported" in await rc.handle_command("91", "register evil m kkkkkkkkk", False)
        assert "Wrong format" in await rc.handle_command("91", "register onlytwo", False)
        up.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_uses_sender_identity():
    with patch("registration_commands.db.delete_user_by_phone",
               new=AsyncMock(return_value=1)) as d:
        assert rc.REMOVED_MSG == await rc.handle_command("919876543210", "remove", False)
        d.assert_awaited_once_with("919876543210")
    with patch("registration_commands.db.delete_user_by_phone",
               new=AsyncMock(return_value=0)):
        assert rc.NOT_FOUND_MSG == await rc.handle_command("919876543210", "remove", False)