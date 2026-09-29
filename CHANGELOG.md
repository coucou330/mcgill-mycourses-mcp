# Changelog

## [2.3.1] - 2026-09-29

### Fixed
- `install_windows.ps1` registered the server in `%APPDATA%\Claude` when no
  config existed yet - but Microsoft Store (MSIX) builds of Claude Desktop read
  `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`. It now
  writes there (creating the file if needed) and restarts Claude Desktop
  (closing the window leaves it running in the tray).
- The installer rejects a non-McGill username (a Gmail/Outlook address fails
  McGill SSO with AADSTS50020) and re-asks on re-run if `.env` has one, keeping
  the calendar URL and dropping the stale session.

### Added
- `testing/selftest_stdio.py`: starts the server over stdio exactly like Claude
  Desktop and calls `hello` + `get_calendar`; the installer runs it before
  registering.

## [2.3.0] - 2026-09-29

### Fixed
- **`get_calendar` showed deadlines in UTC.** The iCal feed is in UTC, so a
  Monday 23:59 (Montreal) deadline was printed as "Tuesday 03:59" - the wrong
  day. Times are now converted to `MYCOURSES_TIMEZONE` (default
  `America/Toronto`) and prefixed with the weekday. All-day events are
  anchored at local midnight so they sort on the right day.
- **`get_calendar` could trigger a login.** Course labeling used to call
  `get_courses()` (Playwright + possibly an MFA pop-up) on every call. Labels
  now come from each event's `LOCATION` (D2L sets it to the full course name),
  with a no-login fallback on a course-id cache written by `get_courses`.
- **Windows:** `setup.py` now finds Python through the `py` launcher (python.org
  installs don't ship `python3.12`, and `python3` is often the Store stub);
  `tzdata` added to requirements (Windows has no system timezone database);
  `.env` is loaded from the script directory whatever the launch cwd, without
  `${...}` interpolation (passwords are taken literally).

### Added
- `install_windows.ps1`: one-command installer (download, Python 3.12 via
  winget, venv, deps, Chromium, `.env` prompts, Claude Desktop config merge
  with backup). Tested: parse, `.env` quoting round-trip through
  python-dotenv, and config merge/idempotency (PowerShell 7).
- `allow_login` argument on `get_courses` / `get_assignments` /
  `get_all_assignments`: `False` returns `LOGIN_REQUIRED` instead of opening
  a login window - for unattended/scheduled assistants.
- `get_all_assignments`: every course's assignments (status/score) in one
  browser session. Composed from the live-verified `get_courses` and
  `get_assignments`; not yet verified live as a whole.
- `get_calendar(course=...)` filter and a direct link to each assignment's
  submission page.
- `claude_desktop_config.windows.example.json`.

## [2.2.0] - 2026-09-03

### Added
- Automatic headless switching for login. `HEADLESS` in `.env` can stay `True`
  permanently: `BrightspaceScraper.login()` detects when a real login is
  actually needed (no cached session, or an expired one) and transparently
  relaunches that one browser instance non-headless right before the MFA
  step — Playwright can't toggle headless mode on a running browser, so
  "switching" means closing and reopening. Every *subsequent* call constructs
  fresh with `headless=True` and just restores the now-valid cached session,
  so a window only ever appears the rare times a real login is genuinely
  required.
- `get_calendar` tool: fetches McGill's personal myCourses iCal feed
  (`MCGILL_ICAL_URL`, from Calendar → Subscribe → "All Calendars and Tasks")
  and returns events in a date window, each labeled with its course code.
  Aggregates every course's calendar in one HTTP call, needs no
  login/MFA/browser at all (the feed URL's token is itself the credential),
  and surfaces deadlines that never appear in the Dropbox/assignments tool
  (content release dates, project milestones, etc.).
  - Course labeling required reverse-engineering where the course id lives in
    the feed: not `UID` (always the feed's own container id, a dead end),
    but a `.../calendar/{course_id}/event/...` link buried in each event's
    `DESCRIPTION`, cross-referenced against `get_courses()`. Institution-wide
    events with no specific course are labeled "General". Labeling is
    best-effort — if the course lookup fails, events still come back, just
    unlabeled.

### Security
- The iCal feed URL's `token` query param is itself a bearer credential —
  documented as password-equivalent in `.env`/README; resettable from
  myCourses' Subscribe dialog if ever exposed.

## [2.1.0] - 2026-09-03

### Fixed (get_assignments — full rewrite)
- The previous draft assumed assignments live behind a link on the course
  homepage, using placeholder selectors that never matched anything on
  McGill's actual UI. McGill's assignments ("Dropbox") live at a distinct
  URL keyed by the course's numeric org-unit id, not discoverable from the
  course page: `/d2l/lms/dropbox/user/folders_list.d2l?ou={course_id}&isprv=0`.
- Rewrote against the real DOM: handles assignments whose title renders as a
  link (open for submission) vs. plain text (not yet open), and cleans up
  score cells that contain hidden duplicate text the page uses for its own
  styling.
- Verified live against multiple real courses, including one the previous
  (broken) selectors had incorrectly reported as having zero assignments.

## [2.0.0] - 2026-09-03

### Changed
- Rebuilt for **McGill myCourses** (`mycourses2.mcgill.ca`) instead of the
  original Purdue Brightspace instance this project started as.
- Replaced Purdue's native-login + Duo Mobile flow with **Microsoft (Entra
  ID) SSO**: a category/SSO selector → Microsoft email/password → MFA →
  optional "Stay signed in?" interstitial.
  - McGill's MFA turned out to be **TOTP code entry** (typed into the page
    yourself), not an out-of-band phone-tap approval as initially assumed —
    this shaped the whole headless/visible-window design above.
  - The SSO entry point looked like a `<button>` but was actually a plain
    `<a href="/d2l/lp/auth/saml/login">` styled to match — role-based
    selectors missed it entirely until targeted by `href` directly.
- Added session persistence (Playwright `storage_state`) so a login (and its
  MFA step) isn't required on every single tool call.
- Actually wired up `mcp_server.py`'s tools to the scraper (`get_courses`,
  `get_assignments`) — the project's MCP integration had previously only
  ever exposed a `hello` connectivity-test tool.
- `get_courses()`'s course-list scraping and course-code parsing both had
  bugs specific to McGill's Brightspace theme/format (a custom element the
  Purdue tenant apparently used that McGill's doesn't, and hyphen- vs.
  space-separated course codes) — both fixed and verified against a live
  account.
- Migrated `mcp_server.py` from the `mcp` Python SDK's 1.x low-level
  `Server` API to 2.x's `MCPServer` (renamed from FastMCP) — the two are not
  API-compatible, and pinned `requirements.txt` accordingly.
- All scraper diagnostics now print to stderr, not stdout — stdout is
  reserved exclusively for MCP's JSON-RPC protocol over stdio, and stray
  prints from a background thread during a live tool call could corrupt it.
- Debug screenshots and the session-cache path now resolve against the
  script's own directory rather than the process's cwd, since an MCP client
  can launch the server from an arbitrary working directory.

### Known limitations
- `get_courses` only sees whichever term tab happens to be active by default
  (usually the current term) — past/future terms need explicit tab
  selection, not yet implemented.
- No grades or announcements support.

## [1.0.0] - 2025-10-14

Initial release, built for Purdue University's Brightspace instance: Duo
Mobile 2FA login, course-list scraping, and initial (unverified,
in-development) assignment-scraping and MCP-server scaffolding.
