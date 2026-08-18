import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from litellm import exceptions as litellm_exc

import db
import evolution
import llm
import registration_commands
from config import settings
from vector_store import store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NOT_REGISTERED_MSG = "You are not registered for this service."
ERROR_MSG = "Sorry, something went wrong processing your message. Please try again."
BAD_CONFIG_MSG = (
    "Your LLM call failed due to your registered settings "
    "(invalid API key, or model not found for your provider). "
    "Please update your registration and try again."
)

MAX_QUESTION_CHARS = 2000        # bounds embedding cost and the user's LLM spend
MAX_BODY_BYTES = 64 * 1024
STORE_LOAD_TIMEOUT_S = 120
UNREG_COOLDOWN_S = 300

# 512MB dyno: cap concurrent in-flight LLM tasks; excess messages queue.
# Cheapest capacity lever — raise toward ~10 if peak-hour replies lag.
llm_semaphore = asyncio.Semaphore(4)

# Cold-start readiness: port binds immediately, store loads in background,
# processing WAITS for readiness (delay, not miss).
store_ready = asyncio.Event()

_unreg_last_reply: dict[str, float] = {}


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


def _extract(payload: dict) -> tuple[str, str, str, bool] | None:
    """Returns (remote_jid, sender_phone, text, is_group) or None.

    remote_jid   -> reply destination (group JID for groups, user JID for DMs)
    sender_phone -> the individual sender. In groups this MUST come from
                    key.participant; remoteJid there is the GROUP id — using it
                    would authorize the group, not the person.
    """
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None
    remote_jid = key.get("remoteJid")
    msg = data.get("message") or {}
    text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
    if not remote_jid or not text:
        return None
    is_group = remote_jid.endswith("@g.us")
    sender_jid = (key.get("participantAlt") or key.get("senderPn") or key.get("participant") or remote_jid) if is_group else remote_jid
    sender_phone = sender_jid.split("@")[0].split(":")[0]   # strip device suffix
    if not sender_phone.isdigit():
        return None
    if is_group:
        candidates = [key.get("participantAlt"), key.get("participant")]
        sender_jid = next((c for c in candidates if c and c.endswith("@s.whatsapp.net")), None)
        if sender_jid is None:
            logger.warning("group msg: no phone JID among participants, domains=%s",
                           [c.split("@")[-1] for c in candidates if c])                                                    # ← add here
        # logger.info("group msg: participant_domain=%s sender_len=%d key_fields=%s",
        #             (key.get("participant") or "").split("@")[-1],
        #             len(sender_phone), list(key.keys()))
            return None
        else:
            sender_jid = remote_jid
    return remote_jid, sender_phone, text.strip()[:MAX_QUESTION_CHARS], is_group


def _should_reply_unregistered(jid: str) -> bool:
    """At most one 'not registered' reply per JID per cooldown window —
    bounds free outbound volume from spam/group noise. In-memory is fine:
    single worker, resets on dyno restart."""
    now = time.monotonic()
    if now - _unreg_last_reply.get(jid, 0) < UNREG_COOLDOWN_S:
        return False
    _unreg_last_reply[jid] = now
    if len(_unreg_last_reply) > 5000:     # bound memory
        _unreg_last_reply.clear()
    return True


async def process_message(remote_jid: str, sender_phone: str, text: str, is_group: bool) -> None:
    try:
        # 1. Commands first — must work for not-yet-registered users, and they
        #    don't need the vector store. `text` may contain an API key:
        #    never logged, never echoed.
        if registration_commands.is_command(text):
            reply = await registration_commands.handle_command(sender_phone, text, is_group)
            await evolution.send_message(remote_jid, reply)
            return

        # 2. Authorization gate — fresh DB read per message (no cache), so an
        #    updated key/provider/model applies to the very next message.
        #    Unregistered/inactive -> cutoff, zero LLM cost.
        cfg = await db.get_user_llm_config(sender_phone)
        if cfg is None:
            if _should_reply_unregistered(remote_jid):
                await evolution.send_message(remote_jid, NOT_REGISTERED_MSG)
            return

        # 3. Cold start: hold until index/model are loaded (delay, not miss).
        try:
            await asyncio.wait_for(store_ready.wait(), timeout=STORE_LOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            await evolution.send_message(remote_jid, ERROR_MSG)
            return

        # 4. RAG + LLM with the sender's own key/provider/model.
        async with llm_semaphore:
            chunks = store.search(text)
            answer = await llm.generate_answer(text, chunks, cfg)
        await evolution.send_message(remote_jid, answer)

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