import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from litellm import exceptions as litellm_exc

import db
import evolution
import llm
import registration_commands
from config import settings
from vector_store import store
from cooldown import CooldownGate

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
    asyncio.create_task(_load_store_background())
    yield
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


def _extract(payload: dict) -> tuple[str, str, str, bool, bool] | None:
    """Returns (remote_jid, sender_phone, text, is_group, mentioned) or None."""
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
        ctx = (data.get("contextInfo") or (ext.get("contextInfo") or {}))
        mentioned_jids = ctx.get("mentionedJid") or []
        bot_nums = _bot_numbers()
        mentioned = any(j.split("@")[0].split(":")[0] in bot_nums for j in mentioned_jids)
        logger.debug("group msg: mention_domains=%s mentioned=%s",
                     [j.split("@")[-1] for j in mentioned_jids], mentioned)
        # logger.info("group msg struct: msg_keys=%s ext_keys=%s",
        #                 list(msg.keys()), list(ext.keys()))
        if mentioned:
            text = _strip_mentions(text)
            if not text:
                return None            # bare tag with no question — nothing to do

    return remote_jid, sender_phone, text[:MAX_QUESTION_CHARS], is_group, mentioned


# async def process_message(remote_jid: str, sender_phone: str, text: str, is_group: bool) -> None:
async def process_message(remote_jid: str, sender_phone: str, text: str, is_group: bool, mentioned: bool) -> None:
    try:
            
        # ---- Group gating (cheapest checks first, before any DB/LLM work) ----
        if is_group:
        # --- dev test ------------------------------------------------------------------------------------------------
            # logger.info("group gate: jid_match=%s mentioned=%s remote=%s",
            #                         remote_jid == settings.allowed_group_jid, mentioned, remote_jid)
        # -------------------------------------------------------------------------------------------------------------
            if remote_jid != settings.allowed_group_jid:
                return                                      # deaf outside the one group
            if not mentioned:
                return                                      # only answer when tagged
            # 1. Commands first — must work for not-yet-registered users, and they
            #    don't need the vector store. `text` may contain an API key:
            #    never logged, never echoed.
            if registration_commands.is_command(text):
                reply = await registration_commands.handle_command(sender_phone, text, is_group=True)
                await evolution.send_message(remote_jid, reply)
                return

            # 2. Authorization gate — fresh DB read per message (no cache), so an
            #    updated key/provider/model applies to the very next message.
            #    Unregistered/inactive -> cutoff, zero LLM cost.
            cfg = await db.get_user_llm_config(sender_phone)
            if cfg is None:
                if unreg_gate.allow(f"{remote_jid}:{sender_phone}"):
                    await evolution.send_message(remote_jid, GROUP_UNREG_MSG)
                return
            await _answer(remote_jid, text, cfg)
            return

        # ---- DM path ----
        if registration_commands.is_command(text):          # commands never cooled down
            reply = await registration_commands.handle_command(sender_phone, text, is_group=False)
            await evolution.send_message(remote_jid, reply)
            return
        if settings.dm_conversation_enabled:
            cfg = await db.get_user_llm_config(sender_phone)
            if cfg is None:
                if unreg_gate.allow(f"dm:{sender_phone}"):
                    await evolution.send_message(remote_jid, NOT_REGISTERED_MSG)
                return
            await _answer(remote_jid, text, cfg)
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
    # import json
    # with open("payload_dump.jsonl", "a") as f:      # TEMPORARY — remove after inspection
    #     f.write(json.dumps(payload) + "\n")
    d = payload.get("data") or {}
    logger.info("inbound key=%s has_alt=%s", 
                {k: v for k, v in (d.get("key") or {}).items() if k != "id"},
                "remoteJidAlt" in (d.get("key") or {}))
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