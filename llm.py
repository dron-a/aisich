import litellm
from datetime import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from app_context import BOT_CONTEXT, SYS_PROMPT_V1, ECHO_PROMPT, ENQUIRE_PROMPT, ENQUIRE_INTENT_PROMPT, SUBJECT_CONTEXT, ECHO_CONTEXT_RULES, EXAM_SCHEDULE

from config import settings
from db import UserLLMConfig
from llm_dispatcher import call_llm
from providers import call_provider
from models import EchoItem, EnquireItem, IntentItem, Prompt
import json

from pydantic import ValidationError

IST = ZoneInfo("Asia/Kolkata")

import logging
logger = logging.getLogger(__name__)

# --- Isolation hardening ---------------------------------------------------
# 1. No env-var fallback: never set provider keys (HUGGINGFACE_API_KEY etc.)
#    as config vars on this app, AND reject empty keys before calling —
#    otherwise LiteLLM silently falls back to env keys (cross-user leak).
# 2. No message/key logging by LiteLLM internals.
litellm.telemetry = False
litellm.turn_off_message_logging = True
litellm.suppress_debug_info = True

# Security boundary, not model validation: arbitrary provider strings can
# route to custom api_base endpoints (SSRF/key-exfiltration vector).
# MUST stay in sync with ALLOWED_PROVIDERS in registration-cf/functions/_lib.ts.
ALLOWED_PROVIDERS = frozenset({"huggingface", "openai", "anthropic", "groq"})

# SYSTEM_PROMPT = f"""You are a helpful assistant for students of the BITS Pilani M.Tech AI & ML programme. Answer their questions using the reference below and any retrieved context provided with the question.

# Rules:
# - Reproduce URLs exactly as written. Never construct, guess, or modify a link.
# - If the answer isn't in the reference or the retrieved context, say you don't know and suggest contacting BITS support at support@wilp.bits-pilani.ac.in. Do not invent facts.
# - Retrieved context reflects peer discussion and may be outdated; the reference below is authoritative where they conflict.
# - Keep answers short — this is WhatsApp.

# ## Reference

# {BOT_CONTEXT}"""
SYSTEM_PROMPT = SYS_PROMPT_V1
# ECHO_PROMPT

class ProviderNotAllowedError(Exception):
    pass

async def generate_answer(question: str, context_chunks: list[str], cfg: UserLLMConfig) -> str:
    if cfg.provider not in ALLOWED_PROVIDERS:
        raise ProviderNotAllowedError(cfg.provider)
    if not cfg.api_key:
        raise ValueError("empty api_key for user")   # never reach env fallback
    if settings.llm_stub:
        return f"[stub] answered from {len(context_chunks)} chunks"

    context = "\n\n---\n\n".join(context_chunks)
    resp = await litellm.acompletion(
        model=f"{cfg.provider}/{cfg.model}",         # wrong model name -> provider
        api_key=cfg.api_key,                         # error -> BAD_CONFIG_MSG (on them)
        timeout=settings.llm_timeout_s,
        max_tokens=512,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"<context>\n{context}\n</context>\n\nQuestion: {question}"},
        ],
    )
    return resp.choices[0].message.content or ""

# -------------------- trial period ------------------------------------------------------------------------------------
async def generate_answer_trial(question: str, context_chunks: list[str]) -> str:
    # Get today's date in your preferred format (e.g., "YYYY-MM-DD")
    today = datetime.now(IST).strftime("%A, %Y-%m-%dT%H:%M:%S%z")
    # datetime.now().strftime("%Y-%m-%d")
    context = "\n\n---\n\n".join(context_chunks)
    # Include it in your prompt structure
    content = (
        f"Today's Date: {today} (IST, +05:30)\n\n"
        f"<context>\n{context}\n</context>\n\n"
        f"Question: {question}"
    )
    prompt = Prompt(system_prompt=SYSTEM_PROMPT, user_prompt=content)
    if settings.local_vector_dir:
        provider = 'gemini'
        resp = await call_llm(provider=provider, prompt=prompt, project=settings.agy_project, location=settings.agy_location, model=settings.agy_model )  
    else:
        resp = await call_provider(prompt=prompt)
    
    
    return resp

async def _generate(prompt: str, model_cls, prefer: str | None = None) -> tuple[list, int]:
    if settings.local_vector_dir:
        provider = 'gemini'
        result = await call_llm(provider=provider, prompt=prompt, project=settings.agy_project, 
                                location=settings.agy_location, model=settings.agy_model)
    else:
        result = await call_provider(prompt=prompt, prefer=prefer)

    raw = result["text"].strip().strip("`").removeprefix("json").strip()

    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]

    items, dropped = [], 0
    for x in data:
        try:
            items.append(model_cls.model_validate(x))
        except ValidationError:
            logger.warning("dropped invalid %s: %r", model_cls.__name__, x)
            dropped += 1
    return items, dropped

async def generate_echo(user_text: str, context_chunks: list[str]) -> tuple[list[EchoItem], int]:
    """Returns (validated items, count dropped as malformed).
    Raises on a response that isn't parseable JSON at all."""
    today = datetime.now(IST).strftime("%A, %Y-%m-%dT%H:%M:%S%z")
    sys_prompt = (ECHO_PROMPT.replace("{SUBJECT_CONTEXT}", SUBJECT_CONTEXT))

    context_block = ""
    if context_chunks:
        context = "\n\n---\n\n".join(context_chunks)
        context_block = f"<context>\n{context}\n</context>\n{ECHO_CONTEXT_RULES}\n\n"

    # Include it in your prompt structure
    content = (
        f"Current date and time: {today}   (IST, +05:30)\n\n" +
        context_block + 
        f"User: {user_text}"
    )
    prompt = Prompt(system_prompt=sys_prompt, user_prompt=content)
    return await _generate(prompt, EchoItem, prefer=settings.ldr_provider)


async def classify_enquire_intent(user_text: str, context_chunks: list[str]) -> tuple[list[IntentItem], int]:
    today = datetime.now(IST).strftime("%A, %Y-%m-%dT%H:%M:%S%z")
    sys_prompt = (ENQUIRE_INTENT_PROMPT.replace("{SUBJECT_CONTEXT}", SUBJECT_CONTEXT))

    context_block = ""
    if context_chunks:
        context = "\n\n---\n\n".join(context_chunks)
        context_block = f"<context>\n{context}\n</context>\n{ECHO_CONTEXT_RULES}\n\n"

    # Include it in your prompt structure
    content = (
        f"Current date and time: {today}   (IST, +05:30)\n\n" +
        context_block +
        f"User: {user_text}"
    )
    prompt = Prompt(system_prompt=sys_prompt, user_prompt=content)
    return await _generate(prompt, IntentItem, prefer=settings.ldr_provider)

async def reply_enquire(usr_context: dict, user_text: str, context_chunks: list[str]) -> str:
    
    today = datetime.now(IST).strftime("%A, %Y-%m-%dT%H:%M:%S%z")
    sys_prompt = (ENQUIRE_PROMPT.replace("{EXAM_SCHEDULE}", EXAM_SCHEDULE))

    context_block = ""
    if context_chunks:
        context = "\n\n---\n\n".join(context_chunks)
        context_block = f"<context>\n{context}\n</context>\n"

    # Include it in your prompt structure
    content = (
        f"Current date and time: {today}   (IST, +05:30)\n\n" +
        context_block +
        f"Student message:\n{user_text}\n\nBundle:\n{json.dumps(usr_context, default=str)}"
    )
    prompt = Prompt(system_prompt=sys_prompt, user_prompt=content)
    return await call_provider(prompt, prefer=settings.ldr_provider)