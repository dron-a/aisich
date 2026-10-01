"""Our own LLM providers, called directly over httpx.

All three speak the OpenAI /chat/completions shape, so there is no translation
layer to pay for -- litellm stays in generate_answer where user-supplied
providers actually need it.

Allocation is exact rather than random: each call goes to whichever available
provider is furthest below its entitled share. Three small lists of state,
sub-microsecond per pick, lost on restart and that is fine.
"""
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

FAILOVER_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

from models import Provider, Prompt

PROVIDERS = (
    Provider(
        "groq",
        settings.groq_url,
        settings.groq_api_key,
        settings.groq_model,
        json_mode=True,
    ),
    Provider(
        "gemini",
        settings.gemini_url,
        settings.gemini_api_key,
        settings.gemini_model,
        json_mode=True,
    ),
    Provider(
        "openrouter",
        settings.ortr_url,
        settings.ortr_api_key,
        settings.ortr_model,
        json_mode=False,
    ),
)

_client: httpx.AsyncClient | None = None


async def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.llm_timeout_s, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def close_client() -> None:
    if _client:
        await _client.aclose()


class _Allocator:
    """Deficit-based weighted allocation across whichever providers are up."""
    __slots__ = ("names", "weights", "counts", "total")

    def __init__(self, spec: str) -> None:
        self.names, self.weights = [], []
        for part in spec.split(","):
            name, _, w = part.partition(":")
            name = name.strip()
            if name:
                self.names.append(name)
                self.weights.append(float(w or 0))
        self.counts = [0.0] * len(self.names)
        self.total = 0
        share = sum(self.weights) or 1.0
        self.weights = [w / share for w in self.weights]

    def pick(self, available: set[str]) -> str | None:
        best_i, best_deficit = None, None
        t = self.total + 1
        for i, name in enumerate(self.names):
            if name not in available:
                continue
            deficit = self.weights[i] * t - self.counts[i]
            if best_deficit is None or deficit > best_deficit:
                best_i, best_deficit = i, deficit
        if best_i is None:
            return None
        self.counts[best_i] += 1
        self.total += 1
        return self.names[best_i]

    def record(self, name: str) -> None:
        """Count a call made outside pick(), so weights still hold overall."""
        try:
            i = self.names.index(name)
        except ValueError:
            return
        self.counts[i] += 1
        self.total += 1


_alloc = _Allocator(settings.llm_weights)
_BY_NAME = {p.name: p for p in PROVIDERS}


async def _try(p, prompt: Prompt,  json_mode: bool, timeout_s: float,
               max_tokens: int, temperature: float) -> tuple[str | None, str]:
    """One attempt. Returns (text, "") on success, (None, reason) on failure."""
    body = {
        "model": p.model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt.system_prompt},
            {"role": "user", "content": prompt.user_prompt},
        ],
    }

    if json_mode and p.json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        r = await _client.post(
            p.base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + p.api_key},
            json=body,
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        logger.warning("provider %s transport error: %s", p.name, exc)
        return None, f"{p.name}: {exc}"

    if r.status_code in FAILOVER_STATUS:
        logger.warning("provider %s returned %s, failing over", p.name, r.status_code)
        return None, f"{p.name}: HTTP {r.status_code}"
    if r.status_code >= 400:
        reason = f"{p.name}: HTTP {r.status_code} {r.text[:200]}"
        logger.warning("provider %s rejected request: %s", p.name, reason)
        return None, reason

    logger.info("provider %s answered", p.name)
    return r.json()["choices"][0]["message"]["content"] or "", ""
    # text = r.json()["choices"][0]["message"]["content"] or ""
    # open("./dev_vectors/llm_raw.log", "a").write(f"{p.name}\t{text}\n")
    # return text, ""


async def call_provider(prompt: Prompt, *, prefer: str | None = None, json_mode: bool = False,
                        timeout_s: float = 90.0, max_tokens: int = 2000,
                        temperature: float = 0, **_) -> str:
    """Try `prefer` first if it is configured, then the rest in allocation
    order until one answers. Raises when all fail, with the last error."""
    if _client is None:
        raise RuntimeError("LLM client not initialized")

    available = {p.name for p in PROVIDERS if p.api_key}
    if not available:
        raise RuntimeError("no provider configured")

    last = "no provider attempted"

    if prefer in available:
        available.discard(prefer)
        _alloc.record(prefer)
        text, last = await _try(_BY_NAME[prefer], prompt, json_mode, timeout_s,
                                max_tokens, temperature)
        if text is not None:
            return text

    while available:
        name = _alloc.pick(available)
        if name is None:
            break
        available.discard(name)
        text, last = await _try(_BY_NAME[name], prompt, json_mode, timeout_s,
                                max_tokens, temperature)
        if text is not None:
            return text

    raise RuntimeError(f"all providers failed ({last})")

# async def call_provider(prompt: Prompt, *, timeout_s: float = 90.0,
#                         max_tokens: int = 2000, temperature: float = 0,
#                         **_) -> str:
#     """Try providers in allocation order until one answers. Raises when all
#     fail, with the last error attached."""
#     if _client is None:
#         raise RuntimeError("LLM client not initialized")

#     available = {p.name for p in PROVIDERS if p.api_key}

#     if not available:
#         raise RuntimeError("no provider configured")

#     last = "no provider attempted"

#     while available:
#         name = _alloc.pick(available)
#         if name is None:
#             break
#         available.discard(name)
#         p = _BY_NAME[name]

#         body = {
#             "model": p.model,
#             "temperature": temperature,
#             "max_tokens": max_tokens,
#             "messages": [
#                 {"role": "system", "content": prompt.system_prompt},
#                 {"role": "user", "content": prompt.user_prompt},
#             ],
#         }
#         if p.json_mode:
#             body["response_format"] = {"type": "json_object"}

#         try:
#             r = await _client.post(
#                 p.base_url + "/chat/completions",
#                 headers={"Authorization": "Bearer " + p.api_key},
#                 json=body,
#                 timeout=timeout_s,

#             )
#         except httpx.HTTPError as exc:
#             last = f"{name}: {exc}"
#             logger.warning("provider %s transport error: %s", name, exc)
#             continue

#         if r.status_code in FAILOVER_STATUS:
#             last = f"{name}: HTTP {r.status_code}"
#             logger.warning("provider %s returned %s, failing over", name, r.status_code)
#             continue
#         if r.status_code >= 400:
#             last = f"{name}: HTTP {r.status_code} {r.text[:200]}"
#             logger.warning("provider %s rejected request: %s", name, last)
#             continue

#         logger.info("provider %s answered", name)
#         return r.json()["choices"][0]["message"]["content"] or ""

#     raise RuntimeError(f"all providers failed ({last})")
