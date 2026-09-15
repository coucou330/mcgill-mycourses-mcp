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
    SCRIPT_DIR,
    log,
    fetch_calendar_events,
    filter_events_by_range,
)

load_dotenv()

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


def _get_credentials() -> tuple[str, str]:
    username = os.getenv("MCGILL_USERNAME")
    password = os.getenv("MCGILL_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "MCGILL_USERNAME and MCGILL_PASSWORD must be set in the .env file "
            "(or in the MCP server's env config)."
        )
    return username, password


def _fetch_courses_sync() -> list[Course]:
    username, password = _get_credentials()
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        if not scraper.login(username, password):
            raise RuntimeError(
                "Login to myCourses failed. Check for a login_*_debug.png "
                f"screenshot in {SCRIPT_DIR} to see exactly which step it "
                "stopped at, or run `python testing/playwright_trial.py` "
                "with HEADLESS=False to debug interactively."
            )
        return scraper.get_courses()


def _course_id_from_url(course_url: str) -> str | None:
    import re
    m = re.search(r'/d2l/home/(\d+)', course_url)
    return m.group(1) if m else None


def _fetch_calendar_sync(days_ahead: int, days_behind: int) -> tuple[list[CalendarEvent], dict[str, str]]:
    ical_url = os.getenv("MCGILL_ICAL_URL")
    if not ical_url:
        raise RuntimeError(
            "MCGILL_ICAL_URL must be set in the .env file. Get it from myCourses: "
            "Calendar > Subscribe > \"All Calendars and Tasks\" > copy the feed URL."
        )
    events = fetch_calendar_events(ical_url)
    events = filter_events_by_range(events, days_ahead=days_ahead, days_behind=days_behind)

    # Best-effort: label each event with its course code. This is the one
    # place get_calendar touches BrightspaceScraper/Playwright at all - if it
    # fails (session expired, etc.) we still return the events, just without
    # course labels, rather than failing the whole call.
    course_map: dict[str, str] = {}
    try:
        for course in _fetch_courses_sync():
            course_id = _course_id_from_url(course.url)
            if course_id:
                course_map[course_id] = course.code
    except Exception as e:
        log(f"get_calendar: could not label events with course codes: {e}")

    return events, course_map


def _fetch_assignments_sync(course_url: str) -> list[Assignment]:
    username, password = _get_credentials()
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        if not scraper.login(username, password):
            raise RuntimeError(
                "Login to myCourses failed. Check for a login_*_debug.png "
                f"screenshot in {SCRIPT_DIR} to see exactly which step it "
                "stopped at, or run `python testing/playwright_trial.py` "
                "with HEADLESS=False to debug interactively."
            )
        return scraper.get_assignments(course_url)


@mcp.tool()
async def hello() -> str:
    """Say hello (connectivity test for the MCP server)"""
    return "Hello from the McGill myCourses MCP server!"


@mcp.tool()
async def get_courses() -> str:
    """List all courses the McGill student is currently enrolled in on myCourses"""
    try:
        courses = await asyncio.to_thread(_fetch_courses_sync)
    except Exception as e:
        return f"Error fetching courses: {e}"

    if not courses:
        return "No courses found."

    lines = [f"- {c.name} ({c.code}) — {c.url}" for c in courses]
    return "Enrolled courses:\n" + "\n".join(lines)


@mcp.tool()
async def get_assignments(course_url: str) -> str:
    """List assignments for a specific myCourses course.

    Args:
        course_url: URL (absolute, or the relative /d2l/home/... path) of the
            course, as returned by get_courses.
    """
    try:
        assignments = await asyncio.to_thread(_fetch_assignments_sync, course_url)
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
async def get_calendar(days_ahead: int = 14, days_behind: int = 1) -> str:
    """Get upcoming (and recently past) events/deadlines across ALL myCourses
    courses at once, via McGill's personal calendar feed.

    This is the preferred tool for "what's due soon" questions - unlike
    get_assignments (which only sees one course's Dropbox at a time), this
    covers every course in a single call and also picks up events that never
    show up in Dropbox at all (content release dates, project milestones,
    etc.). No login/MFA involved - it's a direct, token-authenticated feed.

    Args:
        days_ahead: how many days into the future to include (default 14)
        days_behind: how many days into the past to include (default 1)
    """
    try:
        events, course_map = await asyncio.to_thread(_fetch_calendar_sync, days_ahead, days_behind)
    except Exception as e:
        return f"Error fetching calendar: {e}"

    if not events:
        return f"No events found in the next {days_ahead} days."

    lines = []
    for e in events:
        date_str = e.start.strftime('%Y-%m-%d' if e.all_day else '%Y-%m-%d %H:%M')
        if e.course_id:
            course_label = course_map.get(e.course_id, f"course {e.course_id}")
        else:
            course_label = "General"
        lines.append(f"- [{course_label}] {date_str}: {e.summary}")

    return "Upcoming events:\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
