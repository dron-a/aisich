import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

def _jid_suffix(jid: str) -> str:
    return jid.split("@")[0][-5:]

async def init_client() -> None:
    """Module-level client: reuses TCP/TLS connections across sends."""
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.evolution_api_url,
        headers={"apikey": settings.evolution_api_key},
        timeout=15.0,
    )


async def close_client() -> None:
    if _client:
        await _client.aclose()


async def send_message(remote_jid: str, text: str) -> None:
    assert _client is not None, "HTTP client not initialized"
    r = await _client.post(
        f"/message/sendText/{settings.evolution_instance}",
        json={"number": remote_jid, "text": text},
    )
    if r.status_code >= 400:
        # Log status only — never request/response bodies (PII / keys).
        logger.error("Evolution send failed: status=%s jid_suffix=%s",
                     r.status_code, _jid_suffix(remote_jid))