"""Live Taxila (Moodle) calls.

One shared AsyncClient for the process: connection reuse skips the TLS
handshake on every call after the first, which is worth more than anything
else here against an external host.

Only grades are fetched live -- everything else about a course is synced to
Postgres. A grade is stale within hours and wrong-when-stale in the direction
that matters, so it is never cached.
"""
import asyncio
import logging

import httpx

from config import settings
# from crypto_cache import decrypt_api_key

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

ENDPOINT = "/webservice/rest/server.php"


async def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.taxila_url,
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def close_client() -> None:
    if _client:
        await _client.aclose()


async def _call(wsfunction: str, token: str, **params) -> dict | list:
    if _client is None:
        raise RuntimeError("Taxila client not initialized")
    resp = await _client.post(ENDPOINT, data={
        "wstoken": token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        **params,
    })
    resp.raise_for_status()
    data = resp.json()
    # Moodle returns 200 with an exception body on error.
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(f"{wsfunction}: {data.get('errorcode')}")
    return data


async def _course_grades(user_id: str, token: str, course_id: int) -> list[dict]:
    data = await _call("gradereport_user_get_grade_items", token,
                       userid=user_id, courseid=course_id)

    out = []
    for ug in data.get("usergrades", []):
        for it in ug.get("gradeitems", []):
            out.append({
                "course_id": course_id,
                "module_id": it.get("cmid"),        # joins to calendar + content
                "item": it.get("itemname"),
                "kind": it.get("itemmodule"),       # quiz | assign | None
                "grade": it.get("gradeformatted"),
                "max": it.get("grademax"),
                "graded": it.get("gradedategraded") is not None,
                "hidden": bool(it.get("gradeishidden") or it.get("gradehiddenbydate")),
            })
    return out


async def fetch_grades(user_id: str, wstoken: str,
                       course_ids: list[int]) -> list[dict]:
    """Grade items across the given courses, concurrently. A course that fails
    is logged and skipped -- the rest still return."""
    if not course_ids:
        return []

    # token = decrypt_api_key(wstoken)
    token = wstoken
    results = await asyncio.gather(
        *(_course_grades(user_id, token, cid) for cid in course_ids),
        return_exceptions=True,
    )

    out = []
    for cid, result in zip(course_ids, results):
        if isinstance(result, Exception):
            logger.warning("grades failed for course %s: %s", cid, result)
            continue
        out.extend(result)
    return out
