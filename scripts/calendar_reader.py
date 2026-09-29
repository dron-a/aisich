"""
Read-side model for the calendar table.

For downstream consumers -- an agent, a dashboard, anything that answers
"what is due this week". The bots write this table; nothing here does.

Timezone discipline: Postgres returns TIMESTAMPTZ as tz-aware datetimes in
the *session* timezone, which is not necessarily IST. Every accessor here
converts explicitly before doing date arithmetic, because "is this due
today" has a different answer in two zones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import class_row

IST = ZoneInfo("Asia/Kolkata")

# Every column, named rather than SELECT * -- a new column added by the
# bots would otherwise break construction with an unexpected keyword.


DEADLINE_TYPES = ("assignment", "quiz")
DATED_TYPES = ("assignment", "quiz", "webinar", "class")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One row of the calendar table.

    Field order matches COLUMNS so psycopg's class_row can construct these
    positionally. Keep the two in step.
    """

    event_key: str
    """Stable across polls, which is what makes re-polling idempotent:
    taxila `assign-<cmid>` / `quiz-<coursemodule>`, teams
    `ical-<uid>-<start>`, mail `mail-<uuid>`."""

    source: str
    """taxila | teams | mail"""

    event_type: str
    """assignment | quiz | webinar | class | announcement"""

    subject_key: str
    """Course code as the upstream bot stores it (AIMLZG521), or BITS_WILP
    for anything with no course."""

    title: str
    message: str
    """The line the bot sends over WhatsApp, already formatted."""

    start_date: datetime
    end_date: datetime
    """Equal for classes, webinars and announcements. For assignments and
    quizzes, start is when submissions open and end is the deadline.
    An announcement's dates are both the moment the mail arrived."""

    echo_title: str | None
    """Set once this row has produced a reminder in src_notice, and holds
    the event_key. None means it never has -- either not yet synced, or a
    type that never syncs (class, announcement)."""

    updated_at: datetime
    """Only moves when a field actually changed, not on every poll."""

    # -- derived ------------------------------------------------------

    @property
    def starts(self) -> datetime:
        return self.start_date.astimezone(IST)

    @property
    def ends(self) -> datetime:
        return self.end_date.astimezone(IST)

    @property
    def is_deadline(self) -> bool:
        return self.event_type in DEADLINE_TYPES

    @property
    def has_reminder(self) -> bool:
        return self.echo_title is not None

    @property
    def is_past(self) -> bool:
        return self.ends < datetime.now(IST)

    @property
    def hours_left(self) -> float:
        """Negative once the deadline has passed."""
        return (self.ends - datetime.now(IST)).total_seconds() / 3600

    def on_day(self, day: date) -> bool:
        """True if the event's window covers this IST date. A quiz open for
        a week covers every day in it, not just its endpoints."""
        return self.starts.date() <= day <= self.ends.date()


# ---------------------------------------------------------------------------
# Queries
#
# class_row builds CalendarEvent directly from each row, so nothing here
# hands back raw dicts.
# ---------------------------------------------------------------------------


def _cursor(conn: psycopg.Connection):
    return conn.cursor(row_factory=class_row(CalendarEvent))


def upcoming(
    conn: psycopg.Connection,
    days: int = 14,
    subject_key: str | None = None,
    deadlines_only: bool = False,
) -> list[CalendarEvent]:
    """Anything still open, ordered by how soon it ends."""
    sql = f"""
        SELECT {COLUMNS} FROM calendar
        WHERE end_date >= now()
          AND end_date < now() + make_interval(days => %(days)s)
          AND (%(subject)s IS NULL OR subject_key = %(subject)s)
          AND (NOT %(deadlines)s OR event_type = ANY(%(types)s))
        ORDER BY end_date
    """
    with _cursor(conn) as cur:
        cur.execute(
            sql,
            {
                "days": days,
                "subject": subject_key,
                "deadlines": deadlines_only,
                "types": list(DEADLINE_TYPES),
            },
        )
        return cur.fetchall()


def this_week(conn: psycopg.Connection) -> list[CalendarEvent]:
    """Everything overlapping the current IST week, Monday to Sunday.

    Overlap rather than "ends this week": a quiz that opened last Friday
    and closes next Tuesday is very much this week's problem.
    """
    today = datetime.now(IST).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return in_range(conn, monday, sunday)


def in_range(
    conn: psycopg.Connection, first: date, last: date
) -> list[CalendarEvent]:
    """Events whose window overlaps [first, last] in IST.

    The comparison converts stored timestamps to IST before taking the
    date, so an event ending 00:30 IST counts as that day and not the one
    before -- which is what a naive UTC comparison would give.
    """
    sql = f"""
        SELECT {COLUMNS} FROM calendar
        WHERE (start_date AT TIME ZONE 'Asia/Kolkata')::date <= %(last)s
          AND (end_date   AT TIME ZONE 'Asia/Kolkata')::date >= %(first)s
        ORDER BY end_date
    """
    with _cursor(conn) as cur:
        cur.execute(sql, {"first": first, "last": last})
        return cur.fetchall()


def by_subject(conn: psycopg.Connection, subject_key: str) -> list[CalendarEvent]:
    """Full history for one course, newest first."""
    sql = f"""
        SELECT {COLUMNS} FROM calendar
        WHERE subject_key = %(subject)s
        ORDER BY end_date DESC
    """
    with _cursor(conn) as cur:
        cur.execute(sql, {"subject": subject_key})
        return cur.fetchall()


def get(conn: psycopg.Connection, event_key: str) -> CalendarEvent | None:
    sql = f"SELECT {COLUMNS} FROM calendar WHERE event_key = %s"
    with _cursor(conn) as cur:
        cur.execute(sql, (event_key,))
        return cur.fetchone()


def search(
    conn: psycopg.Connection, term: str, limit: int = 20
) -> list[CalendarEvent]:
    """Case-insensitive substring match on title and message.

    ILIKE, not full-text: the table is small and a trigram or tsvector
    index would be machinery for a few thousand rows.
    """
    sql = f"""
        SELECT {COLUMNS} FROM calendar
        WHERE title ILIKE %(pattern)s OR message ILIKE %(pattern)s
        ORDER BY end_date DESC
        LIMIT %(limit)s
    """
    with _cursor(conn) as cur:
        cur.execute(sql, {"pattern": f"%{term}%", "limit": limit})
        return cur.fetchall()
