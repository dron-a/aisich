"""Unified LLM dispatcher.

One async function, one return shape, three backends selected by a string.
The provider choice is made upstream and passed in.

Return shape: {"text": str, "usd": float | None, "tokens": int | None}
"""
import asyncio
from typing import Any

async def _call_litellm(prompt: str, *, model: str, api_key: str,
                        timeout_s: float, **_) -> dict:
    # import litellm

    # resp = await litellm.acompletion(
    #     model=model,
    #     api_key=api_key,
    #     timeout=timeout_s,
    #     messages=[{"role": "user", "content": prompt}],
    # )
    # usage = getattr(resp, "usage", None)
    # return resp.choices[0].message.content or ""
    return



async def _call_claude(prompt: str, *, timeout_s: float, **_) -> dict:
    return


async def _call_antigravity(prompt: Any, *, project: str, location: str,
                            model: str, timeout_s: float, **_) -> str:
   return

async def _call_gemini(
    prompt: Any,  # Accepts prompt object containing .system_prompt and .user_prompt
    *, 
    project: str, 
    location: str,
    model: str, 
    timeout_s: float, 
    **_  # Silently swallows extra configuration kwargs
) -> str:
    # Inline import for the official unified google-genai library
   return



_HANDLERS = {
    "litellm": _call_litellm,
    "claude": _call_claude,
    "antigravity": _call_antigravity,
    "gemini": _call_gemini,
}


async def call_llm(provider: str, prompt: str, *, timeout_s: float = 90.0, **kwargs) -> dict:
    """provider: "litellm" | "claude" | "antigravity".

    Provider-specific kwargs:
      litellm     - model, api_key
      claude      - none (auth from environment)
      antigravity - project, location, model
    """
    try:
        handler = _HANDLERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider: {provider!r} (choose from {list(_HANDLERS)})")

    return await handler(prompt, timeout_s=timeout_s, **kwargs)