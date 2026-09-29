import json
from datetime import datetime, timedelta
import asyncpg

from config import settings

_pool: asyncpg.Pool | None = None

from models import UserLLMConfig, UserTAXILAConfig, CalendarEvent
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

CAL_COLUMNS = """event_key, source, event_type, subject_key, title, message,
             start_date, end_date, echo_title, updated_at"""


async def _init_conn(conn):
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )

async def init_pool() -> None:
    global _pool
    # Essential-tier Postgres has a low connection cap shared across add-on
    # attachments — keep this pool tiny. One dyno, async I/O: 2 is enough.
    # _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    _pool = await asyncpg.create_pool(
    settings.database_url, min_size=0, max_size=2,   # min_size=0: don't hold idle conns open
    max_inactive_connection_lifetime=180,             # recycle before Neon's ~300s suspend
    init=_init_conn,
    )


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def get_user_llm_config(phone: str) -> UserLLMConfig | None:
    """Read fresh on EVERY message — no caching, so 'update then send'
    always uses the new registration. Sub-1ms via PK index."""
    assert _pool is not None, "DB pool not initialized"
    row = await _pool.fetchrow(
        "SELECT api_key, llm_provider, llm_model FROM users "
        "WHERE phone_number = $1 AND is_active = true",
        phone,
    )
    if not row:
        return None
    return UserLLMConfig(row["api_key"], row["llm_provider"], row["llm_model"])


async def upsert_user_llm_config(phone: str, api_key: str, provider: str, model: str) -> None:
    """Registers or fully overwrites. One row per user (PK); ON CONFLICT makes
    'any change overwrites' atomic — no read-then-write race."""
    assert _pool is not None, "DB pool not initialized"
    await _pool.execute(
        """
        INSERT INTO users (phone_number, api_key, llm_provider, llm_model, is_active)
        VALUES ($1, $2, $3, $4, true)
        ON CONFLICT (phone_number) DO UPDATE SET
            api_key      = EXCLUDED.api_key,
            llm_provider = EXCLUDED.llm_provider,
            llm_model    = EXCLUDED.llm_model,
            is_active    = true
        """,
        phone, api_key, provider, model,
    )


async def delete_user_by_phone(phone: str) -> int:
    """Removes the sender's own registration. Sender identity is the credential."""
    assert _pool is not None, "DB pool not initialized"
    result = await _pool.execute("DELETE FROM users WHERE phone_number = $1", phone)
    return int(result.split()[-1])

async def get_user_taxila_config(phone: str) -> UserTAXILAConfig | None:
    """Read fresh on EVERY message — no caching, so 'update then send'
    always uses the new registration. Sub-1ms via PK index."""
    assert _pool is not None, "DB pool not initialized"
    row = await _pool.fetchrow(
        """
        SELECT user_id, wstoken, teams_url
        FROM dim_taxila_usr
        WHERE phone_number = $1
        AND is_active = true
        """,
        phone
    )
    if not row:
        return None
    return UserTAXILAConfig(row["user_id"], row["wstoken"], row["teams_url"])

async def upsert_user_taxila_config(phone: str, token: str, userid: str) -> None:
    """Registers or fully overwrites. One row per user (PK); ON CONFLICT makes
    'any change overwrites' atomic — no read-then-write race."""
    assert _pool is not None, "DB pool not initialized"
    await _pool.execute(
        """
        INSERT INTO dim_taxila_usr (user_id, wstoken, teams_url, phone, label, is_active, last_error, last_polled_at)
        VALUES ($1, $2, $3, $4, $5, true, $6, $7)
        ON CONFLICT (user_id) DO UPDATE SET
            wstoken  = EXCLUDED.wstoken,
            phone    = EXCLUDED.phone,
            is_active = true
        """,
        userid, token, None, phone, "taxila", None, None
    )

async def upsert_user_msteams_config(phone: str, teams_url: str) -> None:
    """Registers or fully overwrites. One row per user (PK); ON CONFLICT makes
    'any change overwrites' atomic — no read-then-write race."""
    assert _pool is not None, "DB pool not initialized"
    await _pool.execute(
        """
        UPDATE dim_taxila_usr
        SET
            teams_url = $1,
            phone = $2,
            is_active = true,
        WHERE phone = $5
        """,
        teams_url, phone
    )

async def get_calendar_events(subject_keys: list[str] | None = None, echo_title: list[str] | None = None ) -> list[CalendarEvent] | None:
    """Reads events from Calendar"""
    assert _pool is not None, "DB pool not initialized"
    ref = datetime.now(IST)
    st = ref - timedelta(days=15)
    ed = ref + timedelta(days=30)
    args = [st, ed]
    subject_filter = echo_filter = ""
    if subject_keys:
        args.append(subject_keys)
        subject_filter = f" AND subject_key = ANY(${len(args)}::text[])"
    if echo_title:
        args.append(echo_title)
        echo_filter = f" AND echo_title = ANY(${len(args)}::text[])"
    rows = await _pool.fetch(
        f"""
        SELECT {CAL_COLUMNS}
        FROM calendar
        WHERE start_date <= $2
        and end_date >= $1
        {subject_filter} {echo_filter}
        """,
        *args
    )
    if not rows:
        return None
    return [CalendarEvent(**dict(r)) for r in rows]
#------------- tiral period -----------------------------------------
async def trial_consume(phone: str, month: str, quota: int) -> int:
    """Atomic: increments if under quota, returns remaining (>=0) or -1 if exhausted."""
    assert _pool is not None
    row = await _pool.fetchrow(
        """
        INSERT INTO trial_usage (phone, month, count) VALUES ($1, $2, 1)
        ON CONFLICT (phone, month) DO UPDATE SET count = trial_usage.count + 1
            WHERE trial_usage.count < $3
        RETURNING count
        """,
        phone, month, quota,
    )
    return (quota - row["count"]) if row else -1

async def set_notice(item, phone: str, target_group: str) -> str:
    assert _pool is not None, "DB pool not initialized"
    row = await _pool.fetchrow(
        """
        INSERT INTO src_notice (target_group, echo_title, subject_key, message,
                                 start_date, end_date, schedule, notice_kind,
                                 created_by, last_updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
        ON CONFLICT (target_group, echo_title) DO UPDATE SET
            subject_key     = EXCLUDED.subject_key,
            message         = EXCLUDED.message,
            start_date      = EXCLUDED.start_date,
            end_date        = EXCLUDED.end_date,
            schedule        = EXCLUDED.schedule,
            notice_kind     = EXCLUDED.notice_kind,
            last_updated_by = EXCLUDED.last_updated_by
        RETURNING (xmax = 0) AS inserted
        """,
        target_group, item.echo_title, item.subject_key, item.message,
        item.start_date, item.end_date,
        item.schedule.model_dump(exclude_none=True), item.reminder_type, phone,
    )
    return "set" if row["inserted"] else "updated"


async def remove_notice(echo_title: str, phone: str, target_group: str) -> bool:
    assert _pool is not None, "DB pool not initialized"
    row = await _pool.fetchrow(
        "DELETE FROM src_notice WHERE target_group = $1 AND echo_title = $2 and created_by = $3"
        "RETURNING echo_title",
        target_group, echo_title, phone
    )
    return row is not None


async def notice_exists(echo_title: str, phone: str, target_group: str) -> bool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    row = await _pool.fetchrow(
        "SELECT 1 FROM src_notice WHERE target_group = $1 AND echo_title = $2 and created_by = $3",
        target_group, echo_title, phone
    )
    return row is not None


async def stage_update(item, update_message: str, phone: str,
                       target_group: str) -> None:
    assert _pool is not None, "DB pool not initialized"
    await _pool.execute(
        """
        INSERT INTO stg_notice (target_group, echo_title, action_type,
                                 comments, update_message, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (target_group, echo_title, updated_by) DO UPDATE SET
            action_type    = EXCLUDED.action_type,
            comments       = EXCLUDED.comments,
            update_message = EXCLUDED.update_message,
            processed      = FALSE
        """,
        target_group, item.echo_title, item.action_type, item.comments,
        update_message, phone,
    )

async def fetch_current() -> list[dict]:
    """Everything currently worth reporting. Table is pruned elsewhere."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    rows = await _pool.fetch(
        "SELECT echo_title, message FROM current_affairs ORDER BY echo_title"
    )
    return [dict(r) for r in rows]


async def fetch_notices(echo_title: str | None = None,
                        subject_key: str | None = None) -> list[dict]:
    """Reminders, filtered by title if given, else by subject."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")

    cols = "echo_title, message, end_date, schedule"

    if echo_title:
        rows = await _pool.fetch(
            f"SELECT {cols} FROM src_notice WHERE echo_title = $1", echo_title
        )
    elif subject_key:
        rows = await _pool.fetch(
            f"SELECT {cols} FROM src_notice WHERE subject_key = $1 "
            "ORDER BY end_date", subject_key
        )
    else:
        return []

    return [dict(r) for r in rows]

async def fetch_notices_bulk(scope: str, phone: str, titles: list[str],
                             subjects: list[str], target_group: str = None) -> list[dict]:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")

    cols = "echo_title, message, subject_key, end_date, schedule"
    args: list = []
    own = ""
    grp = ""
    if scope == "individual":
        args.append(phone)
        own = f" AND created_by = ${len(args)}"

    if target_group:
        args.append(target_group)
        grp = f" AND target_group = ${len(args)}"

    if titles and subjects:
        args.append(titles); t = len(args)
        args.append(subjects); s = len(args)
        sql = (
            f"SELECT {cols} FROM src_notice WHERE echo_title = ANY(${t}::text[]){grp}{own} "
            f"UNION "
            f"SELECT {cols} FROM src_notice WHERE subject_key = ANY(${s}::text[]){grp}{own} "
            "ORDER BY end_date"
        )
    elif titles:
        args.append(titles); t = len(args)
        sql = (f"SELECT {cols} FROM src_notice WHERE echo_title = ANY(${t}::text[]){grp}{own} "
               "ORDER BY end_date")
    elif subjects:
        args.append(subjects); s = len(args)
        sql = (f"SELECT {cols} FROM src_notice WHERE subject_key = ANY(${s}::text[]){grp}{own} "
               "ORDER BY end_date")
    else:
        return []

    rows = await _pool.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_current_courses(user_id: str,
                              subject_keys: list[str] | None = None) -> list[dict]:
    """Enrolled subjects still running. Resolves subject_key -> course_id for
    everything downstream; an empty result means nothing to fetch."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")

    args: list = [user_id]
    subject_filter = ""
    if subject_keys:
        args.append(subject_keys)
        subject_filter = f" AND subject_key = ANY(${len(args)}::text[])"

    rows = await _pool.fetch(
        f"""
        SELECT course_id, subject_key, semester, fullname, shortname, enddate
        FROM dim_taxila_course
        WHERE user_id = $1
          AND kind = 'subject'
          AND enddate > now()
          {subject_filter}
        ORDER BY enddate
        """,
        *args,
    )
    return [dict(r) for r in rows]


async def get_course_content(course_ids: list[int]) -> list[dict]:
    """Handouts, PYQ folders, slides and activity modules for the given
    courses. Forums are excluded — they carry no artefact or deadline."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    if not course_ids:
        return []

    rows = await _pool.fetch(
        """
        SELECT course_id, module_id, section_name, module_name, modname,
               url, files, dates
        FROM dim_course_content
        WHERE course_id = ANY($1::int[])
          AND modname <> 'forum'
        ORDER BY course_id, section_no, module_id
        """,
        course_ids,
    )
    return [dict(r) for r in rows]


async def get_user_groups(user_id: int, course_ids: list[int]) -> list[dict]:
    """The user's groups in the given courses, with the full roster of each.
    Two passes on the same index: find their group ids, then every member of
    those groups."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    if not course_ids:
        return []

    rows = await _pool.fetch(
        """
        SELECT m.course_id, m.group_id, m.group_name,
               m.user_id, m.fullname, m.bits_id, m.email
        FROM group_memberships m
        WHERE m.group_id IN (
            SELECT group_id FROM group_memberships
            WHERE user_id = $1 AND course_id = ANY($2::int[])
        )
        ORDER BY m.course_id, m.group_id, m.fullname
        """,
        user_id, course_ids,
    )
    return [dict(r) for r in rows]


async def get_user_with_courses(phone: str,
                                subject_keys: list[str] | None = None) -> dict | None:
    """User row plus their current subjects in one round trip. LEFT JOIN so a
    registered user with no synced courses still returns."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")

    args: list = [phone]
    subject_filter = ""
    if subject_keys:
        args.append(subject_keys)
        subject_filter = f" AND c.subject_key = ANY(${len(args)}::text[])"

    rows = await _pool.fetch(
        f"""
        SELECT u.user_id, u.wstoken, u.teams_url,
               c.course_id, c.subject_key, c.fullname
        FROM dim_taxila_usr u
        LEFT JOIN dim_taxila_course c
               ON c.user_id = u.user_id
              AND c.kind = 'subject'
              AND c.enddate > now()
              {subject_filter}
        WHERE u.phone = $1 AND u.is_active
        ORDER BY c.enddate
        """,
        *args,
    )
    if not rows:
        return None
    return {
        "user_id": rows[0]["user_id"],
        "wstoken": rows[0]["wstoken"],
        "teams_url": rows[0]["teams_url"],
        "courses": [
            {"course_id": r["course_id"], "subject_key": r["subject_key"],
             "fullname": r["fullname"]}
            for r in rows if r["course_id"] is not None
        ],
    }

async def get_courses_by_subject(subject_keys: list[str]) -> list[dict]:
    """Course ids for named subjects, from whatever any user has synced.
    Shared course material only -- nothing here is user-specific."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    if not subject_keys:
        return []

    rows = await _pool.fetch(
        """
        SELECT DISTINCT ON (subject_key)
               course_id, subject_key, fullname
        FROM dim_taxila_course
        WHERE kind = 'subject'
          AND enddate > now()
          AND subject_key = ANY($1::text[])
        ORDER BY subject_key, enddate DESC
        """,
        subject_keys,
    )
    return [dict(r) for r in rows]

async def find_notices(target_group: str, phone: str, is_admin: bool,
                       echo_title: str | None = None,
                       subject_key: str | None = None) -> list[dict]:
    """Candidate reminders for an update or remove. Non-admins only ever see
    their own. Narrowest filter the user gave: title, else subject, else all
    of theirs."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")

    args: list = [target_group]
    where = "target_group = $1"

    if echo_title:
        args.append(echo_title)
        where += f" AND echo_title = ${len(args)}"
    if not is_admin:
        args.append(phone)
        where += f" AND created_by = ${len(args)}"
    elif subject_key:
        args.append(subject_key)
        where += f" AND subject_key = ${len(args)}"

    rows = await _pool.fetch(
        f"SELECT echo_title, subject_key, message, end_date "
        f"FROM src_notice WHERE {where} ORDER BY end_date",
        *args,
    )
    return [dict(r) for r in rows]