"""Review and approval of pending engrave chunks.

The daily job writes extracted chunks to a pending file in S3 and never
touches chunks.json. Approving moves selected chunks across; the next job run
rebuilds the index from the approved file only.

Nothing here runs unless an admin asks for it -- no startup cost, no memory
held, no effect on the query path.
"""
import asyncio
import io
import json
import logging

import boto3

from config import settings

logger = logging.getLogger(__name__)

_s3_client = None
PAGE = 5


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _s3_client


def is_admin(phone: str) -> bool:
    return phone in settings.admin_phones


def _get_json(key: str, default):
    buf = io.BytesIO()
    try:
        _s3().download_fileobj(settings.s3_bucket, key, buf)
    except Exception:
        return default
    return json.loads(buf.getvalue() or b"null") or default


def _put_json(key: str, data) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _s3().put_object(Bucket=settings.s3_bucket, Key=key, Body=body)


# --------------------------------------------------------------------- #
# Core operations -- sync (boto3), called via to_thread
# --------------------------------------------------------------------- #
def _list_pending(offset: int = 0) -> dict:
    pending = _get_json(settings.s3_pending_key, [])
    page = pending[offset:offset + PAGE]
    return {
        "total": len(pending),
        "offset": offset,
        "items": page,
        "url": f"s3://{settings.s3_bucket}/{settings.s3_pending_key}",
    }

def _apply_approve(pending: list, approved: list, numbers: list[int]) -> dict:
    if not pending:
        return {"approved": 0, "remaining": 0}

    wanted = {n for n in numbers if 1 <= n <= len(pending)}
    if not wanted:
        return {"approved": 0, "remaining": len(pending), "error": "no valid numbers"}

    taken = [pending[n - 1]["text"] for n in sorted(wanted)]
    left = [p for i, p in enumerate(pending, 1) if i not in wanted]

    seen = set(approved)
    approved.extend(t for t in taken if t not in seen)

    _put_json(settings.s3_chunks_key, approved)     # chunks first
    _put_json(settings.s3_pending_key, left)        # then shrink pending
    return {"approved": len(taken), "remaining": len(left), "total": len(approved)}

def _reject(numbers: list[int]) -> dict:
    pending = _get_json(settings.s3_pending_key, [])
    wanted = {n for n in numbers if 1 <= n <= len(pending)}
    left = [p for i, p in enumerate(pending, 1) if i not in wanted]
    _put_json(settings.s3_pending_key, left)
    return {"rejected": len(wanted), "remaining": len(left)}


# --------------------------------------------------------------------- #
# Async wrappers -- boto3 is blocking
# --------------------------------------------------------------------- #
async def list_pending(offset: int = 0) -> dict:
    return await asyncio.to_thread(_list_pending, offset)

async def approve(numbers: list[int]) -> dict:
    pending, approved = await asyncio.gather(
        asyncio.to_thread(_get_json, settings.s3_pending_key, []),
        asyncio.to_thread(_get_json, settings.s3_chunks_key, []),
    )
    return await asyncio.to_thread(_apply_approve, pending, approved, numbers)

async def reject(numbers: list[int]) -> dict:
    return await asyncio.to_thread(_reject, numbers)


# --------------------------------------------------------------------- #
# WhatsApp command
# --------------------------------------------------------------------- #
def _fmt(result: dict) -> str:
    if not result["total"]:
        return "Nothing pending."
    lines = [f"{result['total']} pending."]
    for i, item in enumerate(result["items"], result["offset"] + 1):
        text = item["text"]
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append(f"\n*{i}.* {text}")
        if item.get("from"):
            lines.append(f"_from {item['from']}_")
    shown = result["offset"] + len(result["items"])
    if shown < result["total"]:
        lines.append(f"\n_review {shown + 1} for more_")
    lines.append("\nReply *approve 1 3* or *reject 2*.")
    return "\n".join(lines)


async def handle_command(phone: str, text: str) -> str | None:
    """Returns a reply, or None if this isn't an admin review command."""
    parts = text.split()
    if not parts or parts[0].lower() != "!review":
        return None
    if not is_admin(phone):
        return None                      # silent: not a command for them

    if len(parts) == 1 or parts[1].isdigit():
        offset = int(parts[1]) - 1 if len(parts) > 1 else 0
        return _fmt(await list_pending(max(offset, 0)))

    action = parts[1].lower()
    numbers = [int(p) for p in parts[2:] if p.isdigit()]
    if not numbers:
        return "Give numbers, e.g. *review approve 1 3*."

    if action == "approve":
        r = await approve(numbers)
        return f"Approved {r['approved']}. {r['remaining']} still pending."
    if action == "reject":
        r = await reject(numbers)
        return f"Rejected {r['rejected']}. {r['remaining']} still pending."
    return "Use *review*, *review approve N*, or *review reject N*."
