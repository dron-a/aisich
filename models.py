from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

@dataclass(frozen=True)
class UserLLMConfig:
    api_key: str
    provider: str
    model: str

    def __repr__(self) -> str:              # defense-in-depth: key never appears
        return "UserLLMConfig(<redacted>)"  # in tracebacks, logs, or debug output

@dataclass(frozen=True)
class UserTAXILAConfig:
    user_id: str
    wstoken: str
    teams_url: str

    def __repr__(self) -> str:              # defense-in-depth: key never appears
        return "UserTAXILAConfig(<redacted>)"  # in tracebacks, logs, or debug output

class Schedule(BaseModel):
    hours: list[int] = Field(min_length=1)
    day_interval: int | None = None
    weekdays: list[int] | None = None
    month_days: list[int] | None = None

    @field_validator("hours")
    @classmethod
    def _hours_range(cls, v):
        if not all(0 <= h <= 23 for h in v):
            raise ValueError("hours must be 0-23")
        return sorted(set(v))

    @field_validator("month_days")
    @classmethod
    def _one_mode(cls, v, info):
        modes = [info.data.get("day_interval"), info.data.get("weekdays"), v]
        if sum(m is not None for m in modes) != 1:
            raise ValueError("exactly one of day_interval/weekdays/month_days")
        return v


class EchoItem(BaseModel):
    valid: bool
    echo_title: str | None = None
    subject_key: str | None = None
    message: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    schedule: Schedule | None = None
    reminder_type: Literal["deadline", "event", "unknown"] | None = None
    comments: str = ""
    action_type: Literal["set", "update", "remove"] | None = None

    @field_validator("echo_title")
    @classmethod
    def _lower(cls, v):
        return v.lower() if v else v

    @field_validator("subject_key")
    @classmethod
    def _upper(cls, v):
        return v.upper() if v else v

class EnquireItem(BaseModel):
    valid: bool
    echo_title: str | None = None
    subject_key: str | None = None
    comments: str = ""
    action_type: Literal["query", "current"] | None = None

    @field_validator("echo_title")
    @classmethod
    def _lower(cls, v):
        return v.lower() if v else v

    @field_validator("subject_key")
    @classmethod
    def _upper(cls, v):
        return v.upper() if v else v

class IntentItem(BaseModel):
    action_type: Literal["query", "reminders"] | None = None
    # valid: bool
    echo_title: list[str] | None = None
    subject_key: list[str] | None = None
    scope: Literal["individual", "general"] | None = None
    comments: str = ""

    @field_validator("echo_title")
    @classmethod
    def _lower(cls, v: list[str] | None):
        return [item.lower() for item in v] if v else v

    @field_validator("subject_key")
    @classmethod
    def _upper(cls, v: list[str] | None):
        return [item.upper() for item in v] if v else v


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

    # @property
    # def is_deadline(self) -> bool:
    #     return self.event_type in DEADLINE_TYPES

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

@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    json_mode: bool

@dataclass(frozen=True)
class Prompt:
    system_prompt: str
    user_prompt: str