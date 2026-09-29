import asyncio
import gzip
import io
import json
from datetime import datetime, timezone, timedelta
import db
from models import EchoItem, EnquireItem, IntentItem
from bundle import build_bundle

IST = timezone(timedelta(hours=5, minutes=30))
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_ITEMS = 12

import boto3

from config import settings

_s3_client = None

_EE_COMMANDS = frozenset({"echo", "enquire", "engrave"})

DM_INVALID_REQ = (
    "⚠️ I cannot register or manager reminders and context on DM. please reach out in the Group."
)
DM_VALID_REQ = (
    "DM is for registration of your own model for your queries in BYOK mode — send 'help' for commands."
)

class ECHO():
    def __init__(self) -> None:
        pass


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


def _put_engrave(phone: str, text: str) -> str:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phone": phone,
        "text": text,
        "kind": "engrave",
    }
    body = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(body)

    now = datetime.now(timezone.utc)
    key = (f"{settings.s3_engrave_prefix}{now:%Y/%m/%d}/"
           f"{now:%H%M%S}-{now.microsecond:06d}.jsonl.gz")
    _s3().put_object(Bucket=settings.s3_bucket, Key=key, Body=buf.getvalue())
    return key


async def save_engrave(phone: str, text: str) -> str:
    """boto3 is sync and blocking — to_thread keeps it off the event loop."""
    return await asyncio.to_thread(_put_engrave, phone, text)

def is_command(text: str) -> bool:
    first = text.split(maxsplit=1)[0].lower() if text else ""
    return first in _EE_COMMANDS

async def handle_command(phone: str, text: str, is_group: bool) -> str:
    """checks for a valid feature suite command and routes action based on the value"""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    body = parts[1]

    if not is_group:
        return DM_INVALID_REQ if cmd in _EE_COMMANDS else DM_VALID_REQ


    if cmd == "engrave":
        if len(body)==0: return "No fact to engrave, correct usage: @<bot tag> engrave <fact>"
        await save_engrave(phone, body)
        return  (cmd, "Noted — the saved context will be available in Bot's KB after the next sync.")

    if cmd in ("echo", "enquire"):
        return  (cmd, body)

async def _resolve(item, phone, target_group) -> tuple[list[str], str | None]:
    if item.echo_title and not item.subject_key and db.notice_exists(item.echo_title, phone, target_group):
        return [item.echo_title], None

    is_admin = phone in settings.admin_phones
    rows = await db.find_notices(target_group, phone, is_admin,
                                 item.echo_title, item.subject_key)
    if not rows:
        what = item.echo_title or item.subject_key or "any reminder"
        return [], f"✗ Couldn't find reminders for {what} that you set."
    if len(rows) == 1:
        return [rows[0]["echo_title"]], None

    # several matched
    if item.action_type == "update" and not item.echo_title:
        return [r["echo_title"] for r in rows], None        # "all my reminders"

    listing = "\n".join(f"• *{r['echo_title']}* — {r['message']}" for r in rows[:8])
    more = f"\n_(+{len(rows) - 8} more)_" if len(rows) > 8 else ""
    return [], f"Which one?\n{listing}{more}\nSay the title."


async def apply_echo(items: list[EchoItem], phone: str, raw_text: str, remote_jid: str) -> str:
    lines = []
    for item in items:
        title = item.echo_title

        if not item.valid:
            lines.append(f"✗ {title or 'request'}: {item.comments}")
            continue

        try:
            if item.action_type == "set":
                what = await db.set_notice(item, phone, remote_jid)
                lines.append(f"✓ {title} {what}. {item.comments}")

            elif item.action_type == "remove":
                title, err = await _resolve(item, phone, remote_jid)
                if err:
                    lines.append(err)
                elif await db.remove_notice(title, phone, remote_jid):
                    lines.append(f"✓ {title} removed.")
                else:
                    lines.append(f"✗ No reminder named {title}.")

            elif item.action_type == "update":
                title, err = await _resolve(item, phone, remote_jid)
                if err:
                    lines.append(err)
                else:
                    item = item.model_copy(update={"echo_title": title})
                    await db.stage_update(item, raw_text, phone, remote_jid)
                    lines.append(f"✓ Update queued for {title}. {item.comments}")

            else:
                lines.append(f"✗ {title or 'request'}: unclear what to do.")

        except Exception:
            # logger.exception("echo %s failed for %s", item.action_type, title)
            lines.append(f"✗ {title or 'request'}: something went wrong.")

    return "\n".join(lines)


def _fmt_hours(hours: list[int]) -> str:
    if len(hours) >= 20:
        return "hourly"
    if len(hours) == 1:
        return f"daily at {hours[0]:02d}:00"
    times = ", ".join(f"{h:02d}:00" for h in hours)
    return f"{len(hours)}x daily at {times}"


def fmt_schedule(sched: dict | None) -> str:
    if not sched or not sched.get("hours"):
        return ""
    base = _fmt_hours(sched["hours"])

    if sched.get("weekdays") is not None:
        days = ", ".join(_DAYS[d] for d in sched["weekdays"] if 0 <= d <= 6)
        return base.replace("daily", f"on {days}") if days else base
    if sched.get("month_days") is not None:
        parts = ["last day" if d == -1 else f"day {d}" for d in sched["month_days"]]
        return f"{base.replace('daily', 'monthly')} ({', '.join(parts)})"

    n = sched.get("day_interval") or 1
    return base.replace("daily", f"every {n} days") if n > 1 else base


def fmt_date(dt) -> str:
    return dt.astimezone(IST).strftime("%-d %b") if dt else ""


def fmt_notices(rows: list[dict]) -> str:
    if not rows:
        return "Nothing found."

    shown, extra = rows[:MAX_ITEMS], len(rows) - MAX_ITEMS
    lines = []
    for r in shown:
        lines.append(f"*{r['echo_title']}* — {r['message']}")
        detail = ", ".join(x for x in (
            f"until {fmt_date(r.get('end_date'))}" if r.get("end_date") else "",
            fmt_schedule(r.get("schedule")),
        ) if x)
        if detail:
            lines.append(detail)
        lines.append("")

    out = "\n".join(lines).rstrip()
    if extra > 0:
        out += f"\n\n(+{extra} more — ask about a specific subject to narrow it down.)"
    return out


def fmt_current(rows: list[dict]) -> str:
    if not rows:
        return "Nothing scheduled right now."

    shown, extra = rows[:MAX_ITEMS], len(rows) - MAX_ITEMS
    lines = [f"*{r['echo_title']}* — {r['message']}" for r in shown]
    out = "\n".join(lines)
    if extra > 0:
        out += f"\n\n(+{extra} more.)"
    return out

# async def prepare_enquire(phone: str, items: list[IntentItem]) -> str:
#     rl, ql = None, None
#     for item in items:
#         if item.valid==True and item.action_type=="reminders":
#             try:
#                 rl = await db.fetch_notices_bulk(item.scope, phone, item.echo_title or [], item.subject_key or [])
#             except Exception:
#                 rl = ["No verified user entry found for reminders"]

#     for item in items:
#         if item.valid==True and item.action_type=="query":
#             try:
#                 cal_records = await db.get_calendar_events(item.subject_key, item.echo_title)
#                 tc = await db.get_user_taxila_config(phone=phone)
#                 if tc is not None:

#                 rl = await db.fetch_notices_bulk(item.scope, phone, item.echo_title or [], item.subject_key or [])
#             except Exception:
#                 rl = ["No verified user entry found for reminders"]
#     if any(i.valid and i.action_type == "current" for i in items):
#         try:
#             blocks.append(fmt_current(await db.fetch_current()))
#         except Exception:
#             # logger.exception("fetch_current failed")
#             blocks.append("✗ Couldn't fetch current items.")

#     titles = [i.echo_title for i in items
#               if i.valid and i.action_type == "query" and i.echo_title]
#     subjects = [i.subject_key for i in items
#                 if i.valid and i.action_type == "query"
#                 and not i.echo_title and i.subject_key]

#     if titles or subjects:
#         try:
#             rows = await db.fetch_notices_bulk(titles, subjects)
#             blocks.append(fmt_notices(rows))
#             # found = {r["echo_title"] for r in rows}
#             # missing = [t for t in titles if t not in found]
#             # if missing:
#             #     blocks.append("Not found: " + ", ".join(missing))
#         except Exception:
#             # logger.exception("fetch_notices_bulk failed")
#             blocks.append("✗ Couldn't fetch reminders.")

#     return "\n\n".join(blocks) if blocks else "Nothing found."


# async def apply_enquire(items: list[IntentItem], phone: str) -> str:
    # usr_ctx = build_bundle(items=items, phone=phone)
    # blocks = [f"✗ {i.comments}" for i in items if not i.valid]
    
    # if any(i.valid and i.action_type == "current" for i in items):
    #     try:
    #         blocks.append(fmt_current(await db.fetch_current()))
    #     except Exception:
    #         # logger.exception("fetch_current failed")
    #         blocks.append("✗ Couldn't fetch current items.")

    # titles = [i.echo_title for i in items
    #           if i.valid and i.action_type == "query" and i.echo_title]
    # subjects = [i.subject_key for i in items
    #             if i.valid and i.action_type == "query"
    #             and not i.echo_title and i.subject_key]

    # if titles or subjects:
    #     try:
    #         rows = await db.fetch_notices_bulk(titles, subjects)
    #         blocks.append(fmt_notices(rows))
    #         # found = {r["echo_title"] for r in rows}
    #         # missing = [t for t in titles if t not in found]
    #         # if missing:
    #         #     blocks.append("Not found: " + ", ".join(missing))
    #     except Exception:
    #         # logger.exception("fetch_notices_bulk failed")
    #         blocks.append("✗ Couldn't fetch reminders.")

    # return "\n\n".join(blocks) if blocks else "Nothing found."
    
