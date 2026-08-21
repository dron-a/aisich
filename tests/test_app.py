import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import main
from db import UserLLMConfig
from main import _extract, app, process_message


def _payload(jid="5511999990000@s.whatsapp.net", text="hello"):
    return {
        "event": "messages.upsert",
        "data": {"key": {"remoteJid": jid, "fromMe": False}, "message": {"conversation": text}},
    }


def _cfg(provider="huggingface", model="mistralai/Mistral-7B-Instruct-v0.3"):
    return UserLLMConfig(api_key="user-key-placeholder", provider=provider, model=model)

@pytest.fixture(autouse=True)
def test_state(monkeypatch):
    main.store_ready.set()
    main.unreg_gate._last.clear()
    main.dm_gate._last.clear()
    main._MENTION_RE = None
    monkeypatch.setattr(main.settings, "dm_conversation_enabled", True)
    monkeypatch.setattr(main.settings, "allowed_group_jid", "12036304@g.us")
    monkeypatch.setattr(main.settings, "bot_number", "919999999999")
    monkeypatch.setattr(main.settings, "bot_lid", "145136239509573")
    yield

# @pytest.fixture(autouse=True)
# def store_is_ready():
#     main.store_ready.set()
#     main.unreg_gate._last.clear()
#     main.dm_gate._last.clear()
#     yield


# ---------- extraction ----------

def test_extract_dm():
    assert _extract(_payload()) == ("5511999990000@s.whatsapp.net", "5511999990000", "hello", False, False)


def test_extract_group_uses_participant_for_auth():
    p = {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": "12036304@g.us", "fromMe": False,
                    "participant": "919876543210@s.whatsapp.net"},
            "message": {"conversation": "hi"},
        },
    }
    remote_jid, sender_phone, text, is_group, mentioned = _extract(p)
    assert remote_jid == "12036304@g.us"      # reply goes to the group
    assert sender_phone == "919876543210"     # auth keys on the PERSON
    assert is_group is True
    assert mentioned is False

def test_extract_group_prefers_phone_jid_over_lid():
    p = {"event": "messages.upsert",
         "data": {"key": {"remoteJid": "12036304@g.us", "fromMe": False,
                          "participant": "98765432101234@lid",
                          "participantAlt": "919876543210@s.whatsapp.net"},
                  "message": {"conversation": "hi"}}}
    _, sender_phone, _, _, _ = main._extract(p)
    assert sender_phone == "919876543210"

def test_extract_ignores_own_and_non_text():
    p = _payload()
    p["data"]["key"]["fromMe"] = True
    assert _extract(p) is None
    assert _extract({"event": "connection.update"}) is None
    assert _extract({"event": "messages.upsert",
                     "data": {"key": {"remoteJid": "x@s.whatsapp.net"}, "message": {}}}) is None


# ---------- authorization gate ----------

@pytest.mark.asyncio
async def test_unregistered_user_gets_cutoff_without_llm_call():
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=None)), \
         patch("main.evolution.send_message", new=AsyncMock()) as send, \
         patch("main.llm.generate_answer", new=AsyncMock()) as gen:
        await process_message("5511999990000@s.whatsapp.net", "5511999990000", "hi", False, False)
        send.assert_awaited_once()
        assert send.await_args.args[1] == main.NOT_REGISTERED_MSG
        gen.assert_not_awaited()              # zero LLM cost for unregistered


@pytest.mark.asyncio
async def test_unregistered_reply_cooldown():
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=None)), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("j@s.whatsapp.net", "1234567", "hi", False, False)
        await process_message("j@s.whatsapp.net", "1234567", "hi again", False, False)
        assert send.await_count == 1          # same sender, cooled down
        # different sender in the same chat is NOT silenced (per-sender fix)
        await process_message("j@s.whatsapp.net", "7654321", "hi", False, False)
        assert send.await_count == 2          # different sender, not silenced
        # send.assert_awaited_once()            # second reply suppressed


# ---------- per-user isolation ----------

@pytest.mark.asyncio
async def test_registered_user_call_uses_that_users_config():
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=_cfg())), \
         patch("main.store.search", return_value=["chunk1"]), \
         patch("main.llm.generate_answer", new=AsyncMock(return_value="answer")) as gen, \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("5511999990000@s.whatsapp.net", "5511999990000", "q", False, False)
        assert gen.await_args.args[2].api_key == "user-key-placeholder"
        assert send.await_args.args[1] == "answer"


@pytest.mark.asyncio
async def test_concurrent_users_do_not_share_keys():
    cfgs = {"1111111": UserLLMConfig("key-A", "huggingface", "m"),
            "2222222": UserLLMConfig("key-B", "openai", "m")}
    seen: list[tuple[str, str]] = []

    async def fake_generate(question, chunks, cfg):
        seen.append((question, cfg.api_key))
        return "ok"

    with patch("main.db.get_user_llm_config", new=AsyncMock(side_effect=lambda p: cfgs[p])), \
         patch("main.store.search", return_value=[]), \
         patch("main.llm.generate_answer", new=AsyncMock(side_effect=fake_generate)), \
         patch("main.evolution.send_message", new=AsyncMock()):
        await asyncio.gather(
            process_message("1111111@s.whatsapp.net", "1111111", "qA", False, False),
            process_message("2222222@s.whatsapp.net", "2222222", "qB", False, False),
        )
    assert ("qA", "key-A") in seen and ("qB", "key-B") in seen


# ---------- user-fixable LLM errors ----------

@pytest.mark.asyncio
async def test_bad_model_returns_actionable_message():
    from litellm import exceptions as litellm_exc
    err = litellm_exc.NotFoundError(message="model not found", model="nope",
                                    llm_provider="huggingface")
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=_cfg(model="nope"))), \
         patch("main.store.search", return_value=[]), \
         patch("main.llm.generate_answer", new=AsyncMock(side_effect=err)), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("5511999990000@s.whatsapp.net", "5511999990000", "q", False, False)
        assert send.await_args.args[1] == main.BAD_CONFIG_MSG


# ---------- cold start ----------

@pytest.mark.asyncio
async def test_message_during_warmup_waits_then_processes():
    main.store_ready.clear()
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=_cfg())), \
         patch("main.store.search", return_value=[]), \
         patch("main.llm.generate_answer", new=AsyncMock(return_value="a")), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        task = asyncio.create_task(
            process_message("91999@s.whatsapp.net", "91999", "q", False, False))
        await asyncio.sleep(0.05)
        assert not task.done()                # held, not dropped, during warmup
        main.store_ready.set()
        await task
        assert send.await_args.args[1] == "a"


@pytest.mark.asyncio
async def test_commands_work_during_warmup():
    main.store_ready.clear()                  # commands don't need the store
    with patch("main.registration_commands.handle_command",
               new=AsyncMock(return_value="done")) as h, \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("91999@s.whatsapp.net", "91999", "help", False, False)
        h.assert_awaited_once()
        assert send.await_args.args[1] == "done"


# ---------- webhook ----------

def test_webhook_rejects_bad_secret():
    with patch("main.settings") as s:
        s.webhook_secret = "expected"
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/webhook", json=_payload(), headers={"x-webhook-secret": "wrong"})
        assert r.status_code == 401


# --------------------behavioural tweaks ----------------------------
def _group_payload(text, mentioned_jids=None, participant_alt="919876543210@s.whatsapp.net"):
    ext = {"text": text}
    if mentioned_jids is not None:
        ext["contextInfo"] = {"mentionedJid": mentioned_jids}
    return {"event": "messages.upsert",
            "data": {"key": {"remoteJid": "12036304@g.us", "fromMe": False,
                             "participant": "98765432101234@lid",
                             "participantAlt": participant_alt},
                     "message": {"extendedTextMessage": ext}}}


def test_extract_group_mention_detected_and_stripped(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_number", "919999999999")
    monkeypatch.setattr(main.settings, "bot_lid", "145136239509573")
    main._MENTION_RE = None
    p = _group_payload("@919999999999 what are the hours?",
                       mentioned_jids=["919999999999@s.whatsapp.net"])
    _, _, text, _, mentioned = main._extract(p)
    assert mentioned is True
    assert text == "what are the hours?"


def test_extract_group_mention_via_lid(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_number", "919999999999")
    monkeypatch.setattr(main.settings, "bot_lid", "55512345678901")
    main._MENTION_RE = None
    p = _group_payload("@55512345678901 hello",
                       mentioned_jids=["55512345678901@lid"])
    _, _, text, _, mentioned = main._extract(p)
    assert mentioned is True and text == "hello"


def test_extract_group_no_mention(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_number", "919999999999")
    main._MENTION_RE = None
    p = _group_payload("just chatting", mentioned_jids=["915550001111@s.whatsapp.net"])
    *_, mentioned = main._extract(p)
    assert mentioned is False


def test_extract_bare_mention_dropped(monkeypatch):
    monkeypatch.setattr(main.settings, "bot_number", "919999999999")
    main._MENTION_RE = None
    assert main._extract(_group_payload("@919999999999",
                         mentioned_jids=["919999999999@s.whatsapp.net"])) is None

# ---------- behavior gating --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_group_outside_allowlist_is_dropped():
    with patch("main.db.get_user_llm_config", new=AsyncMock()) as lookup, \
        patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("999other@g.us", "1234567", "q", True, True)
        lookup.assert_not_awaited()        # dropped before ANY work
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_untagged_is_dropped():
    with patch("main.db.get_user_llm_config", new=AsyncMock()) as lookup, \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("12036304@g.us", "1234567", "q", True, False)
        lookup.assert_not_awaited()
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_tagged_unregistered_gets_register_syntax():
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=None)), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("12036304@g.us", "1234567", "q", True, True)
        assert send.await_args.args[1] == main.GROUP_UNREG_MSG


@pytest.mark.asyncio
async def test_group_tagged_registered_gets_answer():
    with patch("main.db.get_user_llm_config", new=AsyncMock(return_value=_cfg())), \
         patch("main.store.search", return_value=[]), \
         patch("main.llm.generate_answer", new=AsyncMock(return_value="ans")), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("12036304@g.us", "9876543210", "q", True, True)
        assert send.await_args.args[1] == "ans"


@pytest.mark.asyncio
async def test_dm_noncommand_redirected_with_cooldown(monkeypatch):
    monkeypatch.setattr(main.settings, "dm_conversation_enabled", False)
    with patch("main.evolution.send_message", new=AsyncMock()) as send:
        await process_message("91777@s.whatsapp.net", "91777", "what is up", False, False)
        await process_message("91777@s.whatsapp.net", "91777", "hello?", False, False)
        assert send.await_count == 1
        assert send.await_args.args[1] == main.DM_REDIRECT_MSG


@pytest.mark.asyncio
async def test_dm_command_bypasses_cooldown(monkeypatch):
    monkeypatch.setattr(main.settings, "dm_conversation_enabled", False)
    with patch("main.registration_commands.handle_command", new=AsyncMock(return_value="ok")), \
         patch("main.evolution.send_message", new=AsyncMock()) as send:
        for _ in range(3):
            await process_message("91777@s.whatsapp.net", "91777", "help", False, False)
        assert send.await_count == 3       # commands never throttled

def test_extract_group_mention_top_level_contextinfo_lid():
    p = {"event": "messages.upsert",
         "data": {"key": {"remoteJid": "12036304@g.us", "fromMe": False,
                          "participant": "54228458451187@lid",
                          "participantAlt": "919876543210@s.whatsapp.net"},
                  "message": {"conversation": "@145136239509573 what are the hours?"},
                  "contextInfo": {"mentionedJid": ["145136239509573@lid"]}}}
    _, _, text, _, mentioned = main._extract(p)
    assert mentioned is True and text == "what are the hours?"


def test_extract_group_tag_all_not_treated_as_mention():
    p = {"event": "messages.upsert",
         "data": {"key": {"remoteJid": "12036304@g.us", "fromMe": False,
                          "participant": "54228458451187@lid",
                          "participantAlt": "919876543210@s.whatsapp.net"},
                  "message": {"conversation": "@all everyone is tagged"},
                  "contextInfo": {"mentionedJid": [], "nonJidMentions": 1}}}
    *_, mentioned = main._extract(p)
    assert mentioned is False