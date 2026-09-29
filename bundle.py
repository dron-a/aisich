"""Assembles the data bundle for the enquire step-2 LLM call.

Two waves, not three: the user row and their current courses come back in one
joined query, which runs alongside reminders and calendar. Everything that
needs course ids waits on that first wave.

Registration decides reach -- no wstoken means grades, content and groups are
unreachable, so an unregistered  or not taxila_linked user gets reminders and calendar only. Scope
decides ownership on reminders only; calendar rows have no owner.
"""
import asyncio
import logging

import db
import taxila
from config import settings

logger = logging.getLogger(__name__)

from app_context import SUBJECT_NAMES
NO_SUBJECT = "BITS_WILP"


def _gather_keys(items):
    subjects, titles = set(), set()
    want_reminders = want_query = individual = False
    for i in items:
        if i.action_type == "reminders":
            want_reminders = True
        elif i.action_type == "query":
            want_query = True
        if i.scope == "individual":
            individual = True
        for s in i.subject_key or ():
            if s and s != NO_SUBJECT:
                subjects.add(s)
        for t in i.echo_title or ():
            if t:
                titles.add(t)
    return (sorted(subjects) or None, sorted(titles) or None,
            want_reminders, want_query, individual)


async def _run(bundle: dict, tasks: dict) -> None:
    """Run tasks concurrently; a failure degrades that key, never the reply."""
    if not tasks:
        return
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for key, result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.exception("bundle: %s failed", key, exc_info=result)
            bundle["meta"].setdefault("failed", []).append(key)
        elif result:
            bundle[key] = result


async def build_bundle(items, phone: str, target_group: str) -> dict:
    """items: validated EnquireItem list from step 1."""
    subjects, titles, want_reminders, want_query, individual = _gather_keys(items)
    scope = "individual" if individual else "general"

    bundle = {
        "meta": {"taxila_linked": False, "subjects": subjects, "scope": scope},
        "reminders": [], "calendar": [], "grades": [], "content": [], "groups": [],
    }

    # Wave 1 -- nothing here depends on anything else.
    wave1 = {"_usr": db.get_user_with_courses(phone, subjects)}
    if want_reminders:
        wave1["reminders"] = db.fetch_notices_bulk(scope, phone, titles, subjects, target_group)
    if want_reminders or want_query:
        wave1["calendar"] = db.get_calendar_events(subjects, titles)

    await _run(bundle, wave1)
    usr = bundle.pop("_usr", None)
    if not usr:
        bundle["meta"]["courses"] = [
            {"course_id": None, "subject_key": k, "fullname": v}
            for k, v in SUBJECT_NAMES.items()
            if not subjects or k in subjects
        ]
        if settings.allow_freebies and want_query and subjects:   # allow other users to fetch course contents
            courses = await db.get_courses_by_subject(subjects)
            if courses:
                found = {c["subject_key"] for c in courses}
                bundle["meta"]["courses"] = courses + [
                    c for c in bundle["meta"]["courses"]
                    if c["subject_key"] not in found
                ]
                await _run(bundle, {
                    "content": db.get_course_content([c["course_id"] for c in courses]),
                })
        return bundle                      # unregistered or not taxila_linked: calendar + reminders only

    bundle["meta"]["taxila_linked"] = True
    course_ids = [c["course_id"] for c in usr["courses"]]
    bundle["meta"]["courses"] = usr["courses"]

    if not (want_query and course_ids):
        return bundle

    # Wave 2 -- everything keyed on course_id. Grades are the only live call.
    await _run(bundle, {
        "content": db.get_course_content(course_ids),
        "groups": db.get_user_groups(int(usr["user_id"]), course_ids),
        "grades": taxila.fetch_grades(usr["user_id"], usr["wstoken"], course_ids),
    })
    return bundle
