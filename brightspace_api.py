"""
Brightspace MCP Server - Playwright-based web scraping for McGill University (myCourses)
"""

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
import time
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass
from dotenv import load_dotenv
import requests
import icalendar

# Load environment variables
load_dotenv()


def log(*args, **kwargs):
    """
    Print diagnostics to stderr, never stdout.

    When this module runs standalone (python brightspace_api.py), stdout vs
    stderr doesn't matter. But mcp_server.py runs an MCP stdio server, where
    stdout is reserved *exclusively* for JSON-RPC protocol messages - any
    scraper print() leaking onto stdout (this runs in a background thread
    during a live tool call) can corrupt the protocol stream.
    """
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


# Directory this file lives in. Debug screenshots and the default session
# cache use this instead of the process's cwd, since an MCP client (e.g.
# Claude Desktop) may launch this script from an arbitrary working directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _debug_path(filename: str) -> str:
    return os.path.join(SCRIPT_DIR, filename)


# McGill's Brightspace instance
BASE_URL = "https://mycourses2.mcgill.ca"
HOME_URL = f"{BASE_URL}/d2l/home"


@dataclass
class Course:
    """Data class for course information"""
    name: str
    code: str
    instructor: str
    url: str


@dataclass
class Assignment:
    """Data class for assignment information"""
    title: str
    due_date: str
    course: str
    status: str
    url: str


@dataclass
class CalendarEvent:
    """Data class for a myCourses calendar event"""
    summary: str
    start: datetime
    all_day: bool
    uid: str
    course_id: Optional[str]  # numeric org-unit id, same as get_courses()'s
    # Course.url id - None for institution-wide events (course evals, etc.)
    # not tied to any specific course


def fetch_calendar_events(ical_url: str, timeout: int = 30) -> List[CalendarEvent]:
    """
    Fetch and parse McGill's personal myCourses calendar feed.

    This is McGill's own iCal export (Calendar > Subscribe > "All Calendars
    and Tasks" > copy the feed URL) and aggregates events from every one of
    your course calendars in a single feed. Deliberately independent of
    BrightspaceScraper: it's a plain authenticated HTTP GET (the URL's
    `token` query param IS the credential - no cookies, no login, no
    Playwright/browser needed at all), which also makes it far more robust
    than DOM scraping and unaffected by session/cookie expiry.

    SECURITY: the token in ical_url grants read access to the whole calendar
    to anyone who has it - treat it exactly like a password. Never log,
    commit, or share it. If it's ever exposed, reset it from myCourses
    (Calendar > Subscribe > Reset), which invalidates the old URL.

    Args:
        ical_url: the full feed URL including ?token=...

    Returns:
        List[CalendarEvent]: every event in the feed (not date-filtered -
            the feed includes past and future events across the full
            enrollment history). Use filter_events_by_range to narrow it.
    """
    response = requests.get(ical_url, timeout=timeout)
    response.raise_for_status()

    import re

    calendar = icalendar.Calendar.from_ical(response.content)
    events = []
    for component in calendar.walk("VEVENT"):
        dtstart = component.get("dtstart")
        if not dtstart:
            continue
        dt = dtstart.dt
        all_day = not isinstance(dt, datetime)
        if all_day:
            dt = datetime.combine(dt, datetime.min.time(), tzinfo=timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # The event's course isn't in its own field - every course-specific
        # event's DESCRIPTION ends with a "View event -
        # .../calendar/{course_id}/event/..." link. Institution-wide events
        # (course evaluations, etc.) don't have one - course_id stays None.
        # NOTE: UID looks like it might encode this too but doesn't - it's
        # always prefixed with the feed's own container id, not the course's.
        description = str(component.get("description", ""))
        course_match = re.search(r'/calendar/(\d+)/event/', description)

        events.append(CalendarEvent(
            summary=str(component.get("summary", "")),
            start=dt,
            all_day=all_day,
            uid=str(component.get("uid", "")),
            course_id=course_match.group(1) if course_match else None,
        ))

    return events


def filter_events_by_range(
    events: List[CalendarEvent],
    days_ahead: int = 14,
    days_behind: int = 1,
) -> List[CalendarEvent]:
    """Narrow a full event list to a window around now, sorted chronologically."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_behind)
    end = now + timedelta(days=days_ahead)
    filtered = [e for e in events if start <= e.start <= end]
    filtered.sort(key=lambda e: e.start)
    return filtered


class BrightspaceScraper:
    """Main class for scraping McGill myCourses (Brightspace) data"""

    def __init__(self, headless: bool = True, storage_state_path: Optional[str] = None):
        """
        Args:
            headless: preferred mode - run Chromium without a visible window.
                Safe to always leave this True: login() auto-detects when a
                real login is actually needed (no cached session, or an
                expired one) and transparently relaunches THIS scraper
                instance non-headless just for that, since McGill's MFA is a
                TOTP code you read off the Authenticator app and have to type
                INTO THE PAGE yourself - impossible with no window. Once that
                succeeds, the session is cached (storage_state_path) and every
                later BrightspaceScraper (e.g. the next tool call) just
                restores it and stays headless the whole time - no manual
                flipping of this flag required.
            storage_state_path: file to persist/reuse the authenticated session
                (cookies + localStorage) so we don't have to log in - and enter
                a new MFA code - on every single tool call.
        """
        self.headless = headless
        self.storage_state_path = storage_state_path
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def __enter__(self):
        """Context manager entry"""
        self.playwright = sync_playwright().start()
        self._launch(self.headless, restore_session=True)
        return self

    def _launch(self, headless: bool, restore_session: bool):
        """
        (Re-)launch the browser/context/page. Playwright can't toggle headless
        mode on a running browser - it's fixed at launch() time - so
        "switching" means closing the current browser and opening a new one.
        """
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )

        context_kwargs = {}
        if restore_session and self.storage_state_path and os.path.exists(self.storage_state_path):
            log(f"Reusing saved session from {self.storage_state_path}")
            context_kwargs["storage_state"] = self.storage_state_path

        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        self.headless = headless

    def _relaunch(self, headless: bool):
        """Close the current browser and open a new one in the given mode."""
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        self._launch(headless, restore_session=False)

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if hasattr(self, 'playwright'):
            self.playwright.stop()

    def _save_session(self):
        """Persist cookies/localStorage so future runs can skip login + MFA"""
        if self.storage_state_path and self.context:
            self.context.storage_state(path=self.storage_state_path)
            log(f"Session saved to {self.storage_state_path}")

    def _already_logged_in(self) -> bool:
        """Check whether a restored session is still valid"""
        try:
            self.page.goto(HOME_URL)
            self.page.wait_for_load_state("networkidle")
            return "/d2l/home" in self.page.url
        except Exception:
            return False

    def login(self, username: str, password: str) -> bool:
        """
        Login to McGill myCourses via Microsoft (Entra ID / Office 365) SSO.

        McGill's flow differs from a "native" Brightspace login:
          1. myCourses shows a category selector ("Students, Instructors and Staff" /
             Guest / External Users) instead of a plain login form.
          2. Selecting the student/staff option redirects to
             login.microsoftonline.com for the actual authentication.
          3. Microsoft's form is two steps (email, then password).
          4. MFA: McGill's is TOTP code entry - you read a 6-digit code off
             the Authenticator app and type it into the page yourself, then
             click Verify. This needs a visible browser; if constructed with
             headless=True this method auto-relaunches non-headless right
             before this step (see __init__), so you'll see a window pop up
             only when a real login is actually happening. A "Stay signed
             in?" interstitial may follow, which we do dismiss automatically.

        NOTE: The exact selectors below are best-effort. Microsoft's login UI can
        vary slightly by tenant/config. If login fails, run
        `python testing/playwright_trial.py` with HEADLESS=False, watch it step
        through, and adjust the selectors that don't match what you see.

        Args:
            username: McGill username or full email (e.g. you@mcgill.ca)
            password: McGill password

        Returns:
            bool: True if login successful, False otherwise
        """
        try:
            # If we restored a session from disk, check whether it's still valid
            # before doing a full interactive login.
            if self.storage_state_path and os.path.exists(self.storage_state_path):
                if self._already_logged_in():
                    log("Restored session is still valid, skipping login.")
                    return True
                log("Restored session expired, doing a fresh login...")

            # A real login means typing an MFA code into the page - impossible
            # headless. Auto-switch to a visible window right here, only when
            # actually needed. This scraper instance runs non-headless for the
            # rest of its life, but the *next* BrightspaceScraper (next tool
            # call) goes back to headless=True per the caller's default -
            # once this login succeeds and caches the session, that next call
            # just restores it above and never needs a window at all.
            if self.headless:
                log("No valid session - switching to a visible browser window "
                    "for login (McGill's MFA requires typing a code into the page).")
                self._relaunch(headless=False)

            log("Navigating to McGill myCourses...")
            self.page.goto(BASE_URL)
            self.page.wait_for_load_state("networkidle")

            # Step 1: click the "McGill" SSO link. Despite being styled like a
            # button, it's a plain <a id="link1" href="/d2l/lp/auth/saml/login">
            # that kicks off the SAML redirect to Microsoft. Go straight for the
            # href, which is far more stable than the visible label/style.
            log("Looking for the 'McGill' SSO link...")
            clicked = self._click_first([
                "a[href='/d2l/lp/auth/saml/login']",
                "#link1",
                "a:has-text('McGill'):not(:has-text('University'))",
            ])
            if clicked:
                self.page.wait_for_load_state("networkidle")
            else:
                log("Could not find the 'McGill' SSO link - selector may need updating.")
                self.page.screenshot(path=_debug_path("login_mcgill_button_debug.png"))
                return False

            # Step 2: Microsoft email screen (login.microsoftonline.com)
            log("Waiting for Microsoft sign-in page...")
            email_selectors = ["input[name='loginfmt']", "#i0116"]
            email_field = None
            for selector in email_selectors:
                try:
                    email_field = self.page.wait_for_selector(selector, timeout=15000)
                    if email_field:
                        break
                except Exception:
                    continue

            if not email_field:
                log("Could not find Microsoft email field - selectors may need updating.")
                self.page.screenshot(path=_debug_path("login_email_step_debug.png"))
                return False

            log("Entering username/email...")
            # Microsoft accepts either the bare username or full email depending
            # on tenant config; pass through whatever was provided in .env.
            email_field.fill(username)
            self._click_first(["#idSIButton9", "button[type='submit']"])
            self.page.wait_for_load_state("networkidle")

            # Step 3: Microsoft password screen
            log("Waiting for password field...")
            password_selectors = ["input[name='passwd']", "#i0118"]
            password_field = None
            for selector in password_selectors:
                try:
                    password_field = self.page.wait_for_selector(selector, timeout=15000)
                    if password_field:
                        break
                except Exception:
                    continue

            if not password_field:
                log("Could not find Microsoft password field - selectors may need updating.")
                self.page.screenshot(path=_debug_path("login_password_step_debug.png"))
                return False

            log("Entering password...")
            password_field.fill(password)
            self._click_first(["#idSIButton9", "button[type='submit']"])

            # Step 4/5: MFA. McGill's MFA is a TOTP code you read off the
            # Authenticator app and type into the page yourself (not an
            # out-of-band phone-tap approval) - this REQUIRES headless=False
            # so you have a visible window to type into. A "Stay signed in?"
            # interstitial may also follow.
            #
            # IMPORTANT: Microsoft reuses id="idSIButton9" as the primary
            # button on multiple different screens (password, code entry,
            # "stay signed in"). Blindly clicking it here risks submitting
            # the code-entry form while the code field is still empty. So we
            # deliberately do NOT auto-click anything except the exact,
            # unambiguous "Stay signed in?" prompt - everything else (typing
            # the code, hitting Verify) is left for you to do by hand in the
            # visible browser window.
            log("Waiting for MFA / additional verification...")
            log("If a browser window is visible, complete the code entry / "
                "approval there now.")

            deadline = time.time() + 180  # give yourself time to notice + type the code
            reached_home = False
            while time.time() < deadline:
                if "/d2l/home" in self.page.url:
                    reached_home = True
                    break
                try:
                    stay_signed_in = self.page.get_by_text("Stay signed in?", exact=False)
                    if stay_signed_in.is_visible(timeout=500):
                        log("Dismissing 'Stay signed in?' prompt...")
                        self._click_first(["#idSIButton9"], timeout=1000)
                except Exception:
                    pass
                time.sleep(1)

            if reached_home:
                log("Login successful!")
                self._save_session()
                return True
            else:
                log(f"Did not reach myCourses home after login (stuck at {self.page.url})")
                self.page.screenshot(path=_debug_path("login_final_step_debug.png"))
                return False

        except Exception as e:
            log(f"Login error: {e}")
            return False

    def _click_first(self, selectors: List[str], timeout: int = 10000) -> bool:
        """Try a list of selectors in order and click the first one that appears"""
        for selector in selectors:
            try:
                el = self.page.wait_for_selector(selector, timeout=timeout)
                if el:
                    el.click()
                    return True
            except Exception:
                continue
        return False

    def get_courses(self) -> List[Course]:
        """
        Scrape list of enrolled courses

        Returns:
            List[Course]: List of course objects
        """
        try:
            # Navigate to home page
            log("Navigating to homepage...")
            self.page.goto(HOME_URL)
            self.page.wait_for_load_state("networkidle")

            # McGill's theme doesn't wrap courses in a <d2l-my-courses> custom
            # element (unlike some other D2L tenants) - the cards are plain
            # `.d2l-card-container` divs with `a[href*='/d2l/home/']` links
            # inside. Try the custom-element gate as a best-effort speed-up,
            # but don't fail hard if it's absent; fall through to the actual
            # course_selectors below regardless.
            try:
                self.page.wait_for_selector("d2l-my-courses", timeout=5000)
            except Exception:
                pass

            # Wait a bit more for the courses to render
            time.sleep(3)

            # The courses are loaded dynamically, so we need to wait for them
            # Try multiple possible selectors based on the actual HTML structure
            course_selectors = [
                "d2l-enrollment-card .d2l-card-container",  # Main course containers
                ".d2l-card-container",  # Course card containers
                "d2l-enrollment-card a[href*='/d2l/home/']",  # Course links
                "a[href*='/d2l/home/']",  # Any course links
            ]

            courses = []
            for selector in course_selectors:
                log(f"Trying selector: {selector}")
                course_elements = self.page.query_selector_all(selector)

                if course_elements:
                    log(f"Found {len(course_elements)} course elements with {selector}")

                    for element in course_elements:
                        try:
                            # If this is a link element, use it directly
                            if element.evaluate("el => el.tagName.toLowerCase()") == "a":
                                link_element = element
                            else:
                                # Otherwise, look for a link inside this element
                                link_element = element.query_selector("a[href*='/d2l/home/']")

                            if not link_element:
                                continue

                            # Get course URL
                            url = link_element.get_attribute("href")
                            if not url:
                                continue

                            # Get course name from the span inside the link
                            name_span = link_element.query_selector(".d2l-card-link-text")
                            if name_span:
                                name = name_span.text_content().strip()
                            else:
                                # Fallback: get from link text or title
                                name = (
                                    link_element.get_attribute("title") or
                                    link_element.get_attribute("aria-label") or
                                    link_element.text_content()
                                )
                                if name:
                                    name = name.strip()

                            # Extract course code from name
                            course_code = self._extract_course_code(name) if name else "Unknown"

                            if name and name != "":
                                courses.append(Course(
                                    name=name,
                                    code=course_code,
                                    instructor="",
                                    url=url
                                ))
                                log(f"Found course: {name} ({course_code})")
                        except Exception as e:
                            log(f"Error extracting course from element: {e}")
                            continue

                    # If we found courses, don't try other selectors
                    if courses:
                        break

            # If no courses found with any selector, take a screenshot for debugging
            if not courses:
                log("No courses found! Taking screenshot...")
                self.page.screenshot(path=_debug_path("homepage_debug.png"))
                log("Screenshot saved as homepage_debug.png")
                log("Printing page HTML to debug...")
                # Get the inner HTML of the my-courses widget
                my_courses = self.page.query_selector("d2l-my-courses")
                if my_courses:
                    log(f"My Courses widget found, but no course elements inside")
                else:
                    log("My Courses widget not found!")

            return courses

        except Exception as e:
            log(f"Error getting courses: {e}")
            self.page.screenshot(path=_debug_path("error_getting_courses.png"))
            return []

    def get_assignments(self, course_url: str) -> List[Assignment]:
        """
        Scrape assignments (McGill calls this "Dropbox") for a specific course.

        McGill's assignments live at a distinct URL keyed by the course's
        numeric org unit id (the same id in the course's /d2l/home/{id} URL),
        NOT behind a link on the course homepage:
            /d2l/lms/dropbox/user/folders_list.d2l?ou={course_id}&isprv=0

        Args:
            course_url: URL of the course, e.g. "/d2l/home/881533" (as
                returned by get_courses)

        Returns:
            List[Assignment]: List of assignment objects
        """
        try:
            import re

            match = re.search(r'/d2l/home/(\d+)', course_url)
            if not match:
                log(f"Could not extract a course id from course_url: {course_url}")
                return []
            course_id = match.group(1)

            dropbox_url = f"{BASE_URL}/d2l/lms/dropbox/user/folders_list.d2l?ou={course_id}&isprv=0"
            log(f"Navigating to assignments (dropbox) page: {dropbox_url}")
            self.page.goto(dropbox_url)
            self.page.wait_for_load_state("networkidle")

            try:
                self.page.wait_for_selector("table.d_gd", timeout=10000)
            except Exception:
                # No assignments table at all - course may have none posted yet
                log("No assignments table found for this course.")
                return []

            assignments = []
            # Each real assignment is a <tr> containing a .d2l-foldername div;
            # category-header rows and the column-header row don't have one.
            rows = self.page.query_selector_all("table.d_gd tr:has(.d2l-foldername)")

            for row in rows:
                try:
                    # Title: usually a link ("<a><strong>HW01</strong></a>"),
                    # but for assignments not yet open for submission it's
                    # plain text ("<label><strong>HW02</strong></label>") -
                    # scope past both wrapper tags straight to the <strong>.
                    title_el = row.query_selector(".d2l-foldername-medium-font strong")
                    title = title_el.text_content().strip() if title_el else "Unknown"

                    link_el = row.query_selector(".d2l-foldername-medium-font a")
                    assignment_url = link_el.get_attribute("href") if link_el else ""

                    due_el = row.query_selector(".d2l-dates-text strong")
                    due_date = due_el.text_content().strip() if due_el else "No due date"

                    # 3 cells share the ".d_gt" class: completion status, score,
                    # evaluation status (in that order).
                    status_cells = row.query_selector_all("td.d_gt")
                    status = status_cells[0].text_content().strip() if status_cells else "Unknown"

                    if len(status_cells) > 1:
                        # The score cell has hidden duplicate <label>s Playwright's
                        # text_content() still picks up (D2L uses display:none
                        # placeholders) - pull just the "X / Y" pattern back out.
                        score_raw = status_cells[1].text_content()
                        score_match = re.search(r'([\d.\-]+)\s*/\s*([\d.\-]+)', score_raw)
                        if score_match:
                            status = f"{status} ({score_match.group(1)} / {score_match.group(2)})"

                    assignments.append(Assignment(
                        title=title,
                        due_date=due_date,
                        course=course_id,
                        status=status,
                        url=assignment_url
                    ))
                except Exception as e:
                    log(f"Error extracting assignment row: {e}")
                    continue

            return assignments

        except Exception as e:
            log(f"Error getting assignments: {e}")
            self.page.screenshot(path=_debug_path("get_assignments_debug.png"))
            return []

    def _extract_course_code(self, course_name: str) -> str:
        """Extract course code from course name"""
        # McGill formats course codes like "COMP-202", "MATH-240D1-001" (hyphens,
        # not spaces, between the department and the number) - e.g. full names
        # look like "Fall 2026 - COMP-202-001 - Foundations of Programming".
        import re
        match = re.search(r'([A-Z]{3,4})-(\d{3}[A-Z0-9]*)', course_name)
        if match:
            return f"{match.group(1)} {match.group(2)}"
        return course_name.split(' - ')[0] if ' - ' in course_name else course_name

    def _extract_course_from_url(self, url: str) -> str:
        """Extract course name from URL"""
        # Extract course identifier from URL
        parts = url.split('/')
        for part in parts:
            if 'course' in part.lower():
                return part
        return "Unknown Course"

    def save_data(self, data: Dict, filename: str):
        """Save scraped data to JSON file"""
        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
            log(f"Data saved to {filename}")
        except Exception as e:
            log(f"Error saving data: {e}")


def main():
    """Example usage of the Brightspace scraper"""

    # Load credentials from environment variables
    USERNAME = os.getenv("MCGILL_USERNAME")
    PASSWORD = os.getenv("MCGILL_PASSWORD")
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    SESSION_PATH = os.getenv("SESSION_STATE_PATH", ".mcgill_session.json")

    if not USERNAME or not PASSWORD:
        log("❌ Error: MCGILL_USERNAME and MCGILL_PASSWORD must be set in .env file")
        log("Please create a .env file with:")
        log("MCGILL_USERNAME=your_mcgill_username_or_email")
        log("MCGILL_PASSWORD=your_password")
        return

    log(f"Using username: {USERNAME}")

    # Use the scraper with context manager
    with BrightspaceScraper(headless=HEADLESS, storage_state_path=SESSION_PATH) as scraper:
        # Login
        if scraper.login(USERNAME, PASSWORD):
            log("Login successful! Scraping data...")

            # Get courses
            courses = scraper.get_courses()
            log(f"Found {len(courses)} courses:")
            for course in courses:
                log(f"  - {course.name} ({course.code})")

            # Get assignments for first course (if any)
            if courses:
                first_course_url = courses[0].url
                if first_course_url:
                    assignments = scraper.get_assignments(first_course_url)
                    log(f"\nFound {len(assignments)} assignments in {courses[0].name}:")
                    for assignment in assignments:
                        log(f"  - {assignment.title} (Due: {assignment.due_date})")

            # Save data
            data = {
                "courses": [
                    {
                        "name": course.name,
                        "code": course.code,
                        "url": course.url
                    } for course in courses
                ],
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }

            scraper.save_data(data, "brightspace_data.json")

        else:
            log("Login failed!")


if __name__ == "__main__":
    main()
