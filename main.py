import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from litellm import exceptions as litellm_exc
import secrets

import db
import evolution
import llm
import registration_commands
import feature_suite
import taxila
import providers
from bundle import build_bundle
from config import settings
from vector_store import store
from cooldown import CooldownGate
import admin_review

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NOT_REGISTERED_MSG = "You are not registered for this service."
ERROR_MSG = "Sorry, something went wrong processing your message. Please try again."
BAD_CONFIG_MSG = (
    "Your LLM call failed due to your registered settings "
    "(invalid API key, or model not found for your provider). "
    "Please update your registration and try again."
)
GROUP_UNREG_MSG = (
    "You're not registered yet. DM me:\n"
    "register <provider> <model> <api_key>\n"
    "(Never send your API key in a group.)"
)
DM_REDIRECT_MSG = "I answer questions in the group. DM is for registration — send 'help' for commands."
FREE_TRIAL_EXH_MSG = (
    "Free trial exhausted for this month.\n"
    "You can use BYOK mode if you register.\n"
)

MAX_QUESTION_CHARS = 2000        # bounds embedding cost and the user's LLM spend
MAX_BODY_BYTES = 64 * 1024
STORE_LOAD_TIMEOUT_S = 120

import re

_MENTION_RE: re.Pattern | None = None


def _bot_numbers() -> set[str]:
    nums = {settings.bot_number}
    if settings.bot_lid:
        nums.add(settings.bot_lid)
    return nums


def _mention_pattern() -> re.Pattern:
    global _MENTION_RE
    if _MENTION_RE is None:
        alts = "|".join(re.escape(n) for n in _bot_numbers())
        _MENTION_RE = re.compile(rf"@(?:{alts})\b")
    return _MENTION_RE


def _strip_mentions(text: str) -> str:
    return re.sub(r"\s+", " ", _mention_pattern().sub("", text)).strip()

# 512MB dyno: cap concurrent in-flight LLM tasks; excess messages queue.
# Cheapest capacity lever — raise toward ~10 if peak-hour replies lag.
llm_semaphore = asyncio.Semaphore(4)

unreg_gate = CooldownGate(settings.unreg_cooldown_s)
dm_gate = CooldownGate(settings.dm_reply_cooldown_s)

# Cold-start readiness: port binds immediately, store loads in background,
# processing WAITS for readiness (delay, not miss).
store_ready = asyncio.Event()



@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    await evolution.init_client()
    # Do NOT block port binding on model/index load: Heroku kills dynos that
    # don't bind within 60s, and cold start is our hot path (Eco sleep).
    await taxila.init_client()      # httpx -> Taxila
    await providers.init_client()   # httpx -> LLM providers
    asyncio.create_task(_load_store_background())
    yield
    await providers.close_client()
    await taxila.close_client()
    await evolution.close_client()
    await db.close_pool()


async def _load_store_background() -> None:
    try:
        await asyncio.to_thread(store.load)   # blocking S3 + torch init, off the loop
        store_ready.set()
        logger.info("Store ready")
    except Exception:
        # store_ready stays unset -> messages fail with ERROR_MSG after
        # timeout instead of hanging forever.
        logger.exception("Store load failed — dyno cannot serve RAG")


app = FastAPI(lifespan=lifespan)

def _extract(payload: dict) -> tuple[str, str, str, str, bool, bool] | None:
    """Returns (remote_jid, sender_phone, text, quoted, is_group, mentioned) or None."""
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    remote_jid = key.get("remoteJid")
    msg = data.get("message") or {}
    ext = msg.get("extendedTextMessage") or {}
    text = msg.get("conversation") or ext.get("text")
    if not remote_jid or not text:
        return None
    is_group = remote_jid.endswith("@g.us")

    # hoisted out of the group branch -- DMs can quote too
    ctx = (data.get("contextInfo") or ext.get("contextInfo") or {})
    qm = ctx.get("quotedMessage") or {}
    quoted = (qm.get("conversation")
              or (qm.get("extendedTextMessage") or {}).get("text")
              or "")

    if is_group:
        candidates = [key.get("participantAlt"), key.get("participant")]
        sender_jid = next((c for c in candidates if c and c.endswith("@s.whatsapp.net")), None)
        if sender_jid is None:
            logger.warning("group msg: no phone JID among participants, domains=%s",
                           [c.split("@")[-1] for c in candidates if c])
            return None
    else:
        sender_jid = remote_jid
    sender_phone = sender_jid.split("@")[0].split(":")[0]
    if not sender_phone.isdigit():
        return None

    mentioned = False
    if is_group:
        # ctx = (data.get("contextInfo") or (ext.get("contextInfo") or {}))
        mentioned_jids = ctx.get("mentionedJid") or []
        bot_nums = _bot_numbers()
        mentioned = any(j.split("@")[0].split(":")[0] in bot_nums for j in mentioned_jids)
        logger.debug("group msg: mention_domains=%s mentioned=%s",
                     [j.split("@")[-1] for j in mentioned_jids], mentioned)
        # logger.info("group msg struct: msg_keys=%s ext_keys=%s",
        #                 list(msg.keys()), list(ext.keys()))
        if mentioned:
            text = _strip_mentions(text)
            if not text and not quoted:
                return None                   # bare tag and nothing quoted

    return (remote_jid, sender_phone, text[:MAX_QUESTION_CHARS],
            quoted[:MAX_QUESTION_CHARS], is_group, mentioned)

def merge_quote(text: str, quoted: str) -> str:
    if not quoted:
        return text
    if not text:
        return quoted
    return f"{text} — {quoted}"

# async def process_message(remote_jid: str, sender_phone: str, text: str, is_group: bool) -> None:
async def process_message(remote_jid: str, sender_phone: str, text: str, quoted: str, is_group: bool, mentioned: bool) -> None:
    try:
            
        # ---- Group gating (cheapest checks first, before any DB/LLM work) ----
        if is_group:
        # --- dev test ------------------------------------------------------------------------------------------------
            # logger.info("group gate: jid_match=%s mentioned=%s remote=%s",
            #                         remote_jid == settings.allowed_group_jid, mentioned, remote_jid)
        # -------------------------------------------------------------------------------------------------------------
            if remote_jid not in settings.allowed_group_jids:
                return                                      # deaf outside the one group
            if not mentioned:
                return                                      # only answer when tagged
            # 1. Commands first — must work for not-yet-registered users, and they
            #    don't need the vector store. `text` may contain an API key:
            #    never logged, never echoed.
            merged = merge_quote(text, quoted)
            if registration_commands.is_command(text):
                reply = await registration_commands.handle_command(sender_phone, merged, is_group=True)
                await evolution.send_message(remote_jid, reply)
                return

            if feature_suite.is_command(text):
                echoes = await feature_suite.handle_command(sender_phone, merged, is_group=True)
                if echoes[0] == 'engrave':
                    await evolution.send_message(remote_jid, echoes[1])
                    return
                elif echoes[0] == "echo":
                    await _process_echo(echoes[0], remote_jid, echoes[1], sender_phone)
                    return
                elif echoes[0] == "enquire":
                    await _process_enquire(echoes[0], remote_jid, echoes[1], sender_phone)
                    return
                else:
                    logger.warning("unknown cmd: %r", echoes[0])
                    return None
                # result = await _echo_classify(cmd=echoes[0], remote_jid=remote_jid, text=echoes[1])
                # if result is None:
                #     return                      # _echo_classify already messaged the user

                # items, dropped = result
                # summary = await _process_echo(cmd=echoes[0], remote_jid=remote_jid, items=items, phone=sender_phone, raw_text=echoes[1])
                # if not summary:
                #     return                      # _process_echo already messaged, or nothing to say
                # if dropped:
                #     summary += f"\n({dropped} part{'s' if dropped > 1 else ''} couldn't be read.)"
                # await evolution.send_message(remote_jid, summary)
            
            reply = await admin_review.handle_command(sender_phone, merged)
            if reply:
                await evolution.send_message(remote_jid, reply)
                return

            cfg = await db.get_user_llm_config(sender_phone)

            # -------------Trial Period ---------------------------------------------------------------------------------
            if settings.trial_enabled and cfg is None: # registered users never burn trial
                from datetime import datetime, timezone                
                month = datetime.now(timezone.utc).strftime("%Y-%m")
                left = await db.trial_consume(sender_phone, month, settings.trial_quota)
                if left >= 0:
                    await _answer_trial(remote_jid, merged
                                    # db.UserLLMConfig("trial-fake-key", "groq", "llama-3.1-8b-instant")
                                    )
                    return
                if unreg_gate.allow(f"{remote_jid}:{sender_phone}"):
                    await evolution.send_message(remote_jid, FREE_TRIAL_EXH_MSG + GROUP_UNREG_MSG)
                return
            # registered → fall through to normal flow (cfg already fetched)   
            #------------------------------------------------------------------------------------------------------------ 
            # 2. Authorization gate — fresh DB read per message (no cache), so an
            #    updated key/provider/model applies to the very next message.
            #    Unregistered/inactive -> cutoff, zero LLM cost.
            # cfg = await db.get_user_llm_config(sender_phone)
            if cfg is None:
                if unreg_gate.allow(f"{remote_jid}:{sender_phone}"):
                    await evolution.send_message(remote_jid, GROUP_UNREG_MSG)
                return
            await _answer(remote_jid, merged, cfg)
            return

        # ---- DM path ----
        if registration_commands.is_command(text):          # commands never cooled down
            merged = merge_quote(text, quoted)
            reply = await registration_commands.handle_command(sender_phone, merged, is_group=False)
            await evolution.send_message(remote_jid, reply)
            return
        if sender_phone in settings.admin_phones: await _answer_trial(remote_jid, merge_quote(text, quoted)); return
        if settings.dm_conversation_enabled:
            cfg = await db.get_user_llm_config(sender_phone)
            if cfg is None:
                if unreg_gate.allow(f"dm:{sender_phone}"):
                    await evolution.send_message(remote_jid, NOT_REGISTERED_MSG)
                return
            merged = merge_quote(text, quoted)
            await _answer(remote_jid, merged, cfg)
            return
        if dm_gate.allow(sender_phone):
            await evolution.send_message(remote_jid, DM_REDIRECT_MSG)

    except (litellm_exc.AuthenticationError,
            litellm_exc.NotFoundError,
            litellm_exc.BadRequestError):
        # User-fixable: bad key / nonexistent model. Their responsibility per
        # policy — but tell them so they can fix it.
        logger.warning("User-config LLM failure phone_suffix=%s", sender_phone[-4:])
        await evolution.send_message(remote_jid, BAD_CONFIG_MSG)
    except llm.ProviderNotAllowedError:
        logger.error("Disallowed provider phone_suffix=%s", sender_phone[-4:])
        await evolution.send_message(remote_jid, ERROR_MSG)
    except Exception:
        # Never include `text` in logs — it may contain an API key.
        logger.exception("Processing failed phone_suffix=%s", sender_phone[-4:])
        await evolution.send_message(remote_jid, ERROR_MSG)


async def _answer(remote_jid: str, text: str, cfg) -> None:
    """Shared RAG+LLM leg: warmup wait, semaphore, generate, send."""
    try:
        # 3. Cold start: hold until index/model are loaded (delay, not miss).
        await asyncio.wait_for(store_ready.wait(), timeout=STORE_LOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        await evolution.send_message(remote_jid, ERROR_MSG)
        return
    # 4. RAG + LLM with the sender's own key/provider/model.
    async with llm_semaphore:
        chunks = store.search(text)
        answer = await llm.generate_answer(text, chunks, cfg)
    await evolution.send_message(remote_jid, answer)

# ---------- Trial Period ---------------------------------------------------------------------------------
async def _answer_trial(remote_jid: str, text: str) -> None:
    """Shared RAG+LLM leg: warmup wait, semaphore, generate, send."""
    try:
        # 3. Cold start: hold until index/model are loaded (delay, not miss).
        await asyncio.wait_for(store_ready.wait(), timeout=STORE_LOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        await evolution.send_message(remote_jid, ERROR_MSG)
        return
    # 4. RAG + LLM with the sender's own key/provider/model.
    async with llm_semaphore:
        chunks = await asyncio.to_thread(store.search, text)
        answer = await llm.generate_answer_trial(text, chunks)
    await evolution.send_message(remote_jid, answer)

    
async def _process_echo(cmd: str, remote_jid: str, text: str, phone: str) -> None:
    """processes echo suite commands for reminder management"""
    try:
        # 3. Cold start: hold until index/model are loaded (delay, not miss).
        await asyncio.wait_for(store_ready.wait(), timeout=STORE_LOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        await evolution.send_message(remote_jid, ERROR_MSG)
        return
    # 4. RAG + LLM with the sender's own key/provider/model.
    async with llm_semaphore:
        # chunks = await asyncio.to_thread(store.search, text)
        chunks = []
        try:
            items, dropped = await llm.generate_echo(text, chunks)
        except Exception:
            logger.exception("%s generation failed", cmd)
            await evolution.send_message(remote_jid, "Couldn't read that — try rephrasing?")
            return None

    if not items:
        await evolution.send_message(remote_jid, "Couldn't read that — try rephrasing?")
        return None
    try:
        processed = await feature_suite.apply_echo(items, phone, text, remote_jid)
    except Exception:
        logger.exception("%s apply failed for commnad", cmd)
        await evolution.send_message(remote_jid, "Sorry Couldn't manage that reminder request, please try again")
        return None
    if not processed:
        await evolution.send_message(remote_jid, "This Request Failed when I tried to process the reminder, please try again")
        return
    if dropped:
        processed += f"\n({dropped} part{'s' if dropped > 1 else ''} couldn't be read.)"
    await evolution.send_message(remote_jid, processed)
    return

async def _process_enquire(cmd: str, remote_jid: str, text: str, phone: str) -> None:
    """processes echo suite commands for reminder management"""
    try:
        # 3. Cold start: hold until index/model are loaded (delay, not miss).
        await asyncio.wait_for(store_ready.wait(), timeout=STORE_LOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        await evolution.send_message(remote_jid, ERROR_MSG)
        return
    # 4. RAG + LLM with the sender's own key/provider/model.
    async with llm_semaphore:
        # chunks = await asyncio.to_thread(store.search, text)
        chunks = []
        try:
            items, _ = await llm.classify_enquire_intent(text, chunks)
        except Exception:
            logger.exception("%s generation failed", cmd)
            await evolution.send_message(remote_jid, "Couldn't read that — try rephrasing?")
            return None

    if not items:
        await evolution.send_message(remote_jid, "Couldn't read that — try rephrasing?")
        return None
    if len(items)==1 and items[0].action_type is None:
        await evolution.send_message(remote_jid, f"{items[0].comments}, please rephrase your request")
        return None

    try:
        usr_ctx = await build_bundle(items=items, phone=phone, target_group=None)
        async with llm_semaphore:
            response = await llm.reply_enquire(usr_ctx, text, chunks)
            await evolution.send_message(remote_jid, response)
            return None
    except Exception:
                logger.exception("%s apply failed for commnad", cmd)
                await evolution.send_message(remote_jid, "Sorry Couldn't reply to that request, please try again")
                return None

# -----------------------------------------------------------------------------------------------------------
def _auth(token: str):
    if not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(403)

@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str = Header(default=""),
):
    if not hmac.compare_digest(x_webhook_secret, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    payload = await request.json()
    import json
    # logger.info("PAYLOAD %s", json.dumps(payload))
    with open("dev_vectors/payload_dump1.jsonl", "a") as f:      # TEMPORARY — remove after inspection
        f.write(json.dumps(payload) + "\n")
    # d = payload.get("data") or {}
    # logger.info("inbound key=%s has_alt=%s", 
    #             {k: v for k, v in (d.get("key") or {}).items() if k != "id"},
                # "remoteJidAlt" in (d.get("key") or {}))
    extracted = _extract(payload)
    if extracted:
        background_tasks.add_task(process_message, *extracted)
    # Instant 200 so Evolution doesn't retry while processing runs.
    return {"status": "received"}


@app.get("/health")
async def health():
    # Honest readiness: an uptime ping must not mask a failed store load.
    return {
        "status": "ok" if store_ready.is_set() else "warming",
        "chunks_loaded": len(store.chunks),
    }

@app.post("/admin/reload-store")
async def reload_store(x_admin_token: str = Header(...)):
    _auth(x_admin_token)
    await asyncio.to_thread(store.reload)
    return {"chunks": store.index.size}