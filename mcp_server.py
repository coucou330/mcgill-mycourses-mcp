#!/usr/bin/env python3
"""
MCP server exposing McGill myCourses (Brightspace) data to Claude Desktop.
"""

import asyncio
import os
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from brightspace_api import (
    BrightspaceScraper,
    Course,
    Assignment,
    CalendarEvent,
    LoginRequired,
    SCRIPT_DIR,
    log,
    fetch_calendar_events,
    filter_events_by_range,
    get_local_tz,
    course_id_from_url,
    save_course_cache,
    load_course_cache,
)

load_dotenv(os.path.join(SCRIPT_DIR, ".env"), interpolate=False)

# Resolve relative to this file's directory, not the process cwd - an MCP
# client (e.g. Claude Desktop) may launch this script from anywhere.
_session_path_env = os.getenv("SESSION_STATE_PATH", ".mcgill_session.json")
SESSION_PATH = (
    _session_path_env
    if os.path.isabs(_session_path_env)
    else os.path.join(SCRIPT_DIR, _session_path_env)
)
HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"

mcp = MCPServer("mycourses-mcgill")

_LOGIN_FAILED = (
    "Login to myCourses failed. Check for a login_*_debug.png "
    f"screenshot in {SCRIPT_DIR} to see exactly which step it "
    "stopped at, or run `python testing/playwright_trial.py` "
    "with HEADLESS=False to debug interactively."
)


def _get_credentials() -> tuple[str, str]:
    username = os.getenv("MCGILL_USERNAME")
    password = os.getenv("MCGILL_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "MCGILL_USERNAME and MCGILL_PASSWORD must be set in the .env file "
            "(or in the MCP server's env config)."
        )
    return username, password


def _login_or_raise(scraper: BrightspaceScraper, allow_login: bool) -> None:
    username, password = _get_credentials()
    if not scraper.login(username, password, allow_interactive=allow_login):
        raise RuntimeError(_LOGIN_FAILED)


def _fetch_courses_sync(allow_login: bool = True) -> list[Course]:
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        _login_or_raise(scraper, allow_login)
        courses = scraper.get_courses()
    save_course_cache(courses)
    return courses


def _fetch_assignments_sync(course_url: str, allow_login: bool = True) -> list[Assignment]:
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        _login_or_raise(scraper, allow_login)
        return scraper.get_assignments(course_url)


def _fetch_all_assignments_sync(allow_login: bool = True) -> list[tuple[Course, list[Assignment]]]:
    # One browser + one login for every course, instead of one per course.
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        _login_or_raise(scraper, allow_login)
        courses = scraper.get_courses()
        results = [(c, scraper.get_assignments(c.url)) for c in courses]
    save_course_cache(courses)
    return results


def _fetch_calendar_sync(days_ahead: int, days_behind: int) -> list[CalendarEvent]:
    ical_url = os.getenv("MCGILL_ICAL_URL")
    if not ical_url:
        raise RuntimeError(
            "MCGILL_ICAL_URL must be set in the .env file. Get it from myCourses: "
            "Calendar > Subscribe > \"All Calendars and Tasks\" > copy the feed URL."
        )
    events = fetch_calendar_events(ical_url)
    return filter_events_by_range(events, days_ahead=days_ahead, days_behind=days_behind)


def _course_label(e: CalendarEvent, course_map: dict[str, str]) -> str:
    # 1) the event's own LOCATION (course name) - always there for course events
    # 2) the course-id cache written by get_courses - never triggers a login
    # 3) raw id / "General" for institution-wide events
    if e.course_code:
        return e.course_code
    if e.course_id:
        return course_map.get(e.course_id, f"course {e.course_id}")
    return "General"


def _format_when(e: CalendarEvent) -> str:
    if e.all_day:
        # All-day events are stored as midnight UTC: use the date as-is, a tz
        # conversion would shift it to the previous evening in Montreal.
        return e.start.strftime("%a %Y-%m-%d") + " (all day)"
    return e.start.astimezone(get_local_tz()).strftime("%a %Y-%m-%d %H:%M")


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _login_required_msg(e: LoginRequired) -> str:
    return f"LOGIN_REQUIRED: {e}"


@mcp.tool()
async def hello() -> str:
    """Say hello (connectivity test for the MCP server)"""
    return "Hello from the McGill myCourses MCP server!"


@mcp.tool()
async def get_courses(allow_login: bool = True) -> str:
    """List all courses the McGill student is currently enrolled in on myCourses.

    Args:
        allow_login: if the cached session has expired, open a browser window
            so the student can log in (MFA code). Pass False for unattended /
            scheduled runs: the tool then returns LOGIN_REQUIRED instead of
            waiting for someone to type a code.
    """
    try:
        courses = await asyncio.to_thread(_fetch_courses_sync, allow_login)
    except LoginRequired as e:
        return _login_required_msg(e)
    except Exception as e:
        return f"Error fetching courses: {e}"

    if not courses:
        return "No courses found."

    lines = [f"- {c.name} ({c.code}) — {c.url}" for c in courses]
    return "Enrolled courses:\n" + "\n".join(lines)


@mcp.tool()
async def get_assignments(course_url: str, allow_login: bool = True) -> str:
    """List assignments (due date, submission status, score) for ONE course.

    Args:
        course_url: URL (absolute, or the relative /d2l/home/... path) of the
            course, as returned by get_courses.
        allow_login: see get_courses. Pass False for unattended runs.
    """
    try:
        assignments = await asyncio.to_thread(_fetch_assignments_sync, course_url, allow_login)
    except LoginRequired as e:
        return _login_required_msg(e)
    except Exception as e:
        return f"Error fetching assignments: {e}"

    if not assignments:
        return "No assignments found for that course."

    lines = [
        f"- {a.title} (Due: {a.due_date}, Status: {a.status})"
        for a in assignments
    ]
    return "Assignments:\n" + "\n".join(lines)


@mcp.tool()
async def get_all_assignments(allow_login: bool = True) -> str:
    """List assignments (due date, submission status, score) for EVERY enrolled
    course in one call - one browser session and one login for all courses.
    Use this for "what's left to hand in?" / "where am I in each course?".

    Args:
        allow_login: see get_courses. Pass False for unattended runs.
    """
    try:
        results = await asyncio.to_thread(_fetch_all_assignments_sync, allow_login)
    except LoginRequired as e:
        return _login_required_msg(e)
    except Exception as e:
        return f"Error fetching assignments: {e}"

    if not results:
        return "No courses found."

    out = []
    for course, assignments in results:
        out.append(f"## {course.code} — {course.name}")
        if not assignments:
            out.append("- (no assignments found)")
        for a in assignments:
            out.append(f"- {a.title} (Due: {a.due_date}, Status: {a.status})")
    return "\n".join(out)


@mcp.tool()
async def get_calendar(days_ahead: int = 14, days_behind: int = 1, course: str = "") -> str:
    """Get upcoming (and recently past) events/deadlines across ALL myCourses
    courses at once, via McGill's personal calendar feed.

    This is the preferred tool for "what's due soon" questions - unlike
    get_assignments (which only sees one course's Dropbox at a time), this
    covers every course in a single call and also picks up events that never
    show up in Dropbox at all (content release dates, project milestones,
    etc.). No login/MFA/browser involved - it's a direct, token-authenticated
    feed, so it is safe for unattended runs. Times are shown in the local
    timezone (MYCOURSES_TIMEZONE, default America/Toronto).

    Args:
        days_ahead: how many days into the future to include (default 14)
        days_behind: how many days into the past to include (default 1)
        course: optional filter, e.g. "ECSE 307" or "ecse307" (matches the
            course code or name)
    """
    try:
        events = await asyncio.to_thread(_fetch_calendar_sync, days_ahead, days_behind)
    except Exception as e:
        return f"Error fetching calendar: {e}"

    course_map = load_course_cache()
    if course:
        wanted = _norm(course)
        events = [
            e for e in events
            if wanted in _norm(_course_label(e, course_map)) or wanted in _norm(e.course_name or "")
        ]

    if not events:
        scope = f" for {course}" if course else ""
        return f"No events found{scope} in the next {days_ahead} days."

    tz = get_local_tz()
    lines = []
    for e in events:
        line = f"- [{_course_label(e, course_map)}] {_format_when(e)}: {e.summary}"
        if e.url:
            line += f" — {e.url}"
        lines.append(line)

    return f"Upcoming events (times in {getattr(tz, 'key', 'UTC')}):\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
