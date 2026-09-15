"""
Simple Playwright test script for McGill myCourses (Microsoft SSO login).

Run this non-headless first to confirm/fix the selectors before relying on
mcp_server.py or brightspace_api.py. It walks through each step and takes a
screenshot, so if a selector is wrong for your tenant you can see exactly
where it broke.
"""

from playwright.sync_api import sync_playwright
import time
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BASE_URL = "https://mycourses2.mcgill.ca"


def test_mycourses_access():
    """Test basic access to McGill myCourses"""

    # Get credentials from environment variables
    username = os.getenv("MCGILL_USERNAME")
    password = os.getenv("MCGILL_PASSWORD")

    if not username or not password:
        print("❌ Error: MCGILL_USERNAME and MCGILL_PASSWORD must be set in .env file")
        return

    print(f"Using username: {username}")

    with sync_playwright() as p:
        # Launch browser (keep visible for manual interaction / debugging)
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        try:
            # Navigate to McGill myCourses
            print("Navigating to McGill myCourses...")
            page.goto(BASE_URL)

            # Wait for page to load
            page.wait_for_load_state("networkidle")

            # Take screenshot of initial page
            page.screenshot(path="mycourses_initial.png")
            print("Screenshot saved as mycourses_initial.png")

            # Click the "McGill" SSO link. Despite being styled like a button,
            # it's a plain <a id="link1" href="/d2l/lp/auth/saml/login"> that
            # kicks off the SAML redirect to Microsoft - target the href, which
            # is far more stable than the visible label/CSS styling.
            print("Looking for the 'McGill' SSO link...")
            clicked = False
            for selector in ["a[href='/d2l/lp/auth/saml/login']", "#link1"]:
                el = page.query_selector(selector)
                if el:
                    print(f"✓ Found McGill SSO link via: {selector}")
                    el.click()
                    page.wait_for_load_state("networkidle")
                    clicked = True
                    break

            if not clicked:
                print("✗ McGill SSO link not found - inspect mycourses_initial.png "
                      "and adjust the selectors above in this script.")

            page.screenshot(path="mycourses_after_category_click.png")
            print("Screenshot saved as mycourses_after_category_click.png")
            print(f"Current URL: {page.url}")

            # Now check if we've been redirected to Microsoft's login page
            print("Checking for Microsoft email field...")
            try:
                email_field = None
                for selector in ["input[name='loginfmt']", "#i0116"]:
                    email_field = page.query_selector(selector)
                    if email_field:
                        print(f"✓ Found email field via: {selector}")
                        break

                if not email_field:
                    print("✗ Email field not found. Are you already signed in to a "
                          "Microsoft account in this browser? Check "
                          "mycourses_after_category_click.png")
                    return

                # Fill in email/username
                print("Entering email/username...")
                email_field.fill(username)
                page.click("#idSIButton9")
                page.wait_for_load_state("networkidle")

                page.screenshot(path="mycourses_after_email.png")
                print("Screenshot saved as mycourses_after_email.png")

                # Fill in password
                password_field = None
                for selector in ["input[name='passwd']", "#i0118"]:
                    password_field = page.query_selector(selector)
                    if password_field:
                        print(f"✓ Found password field via: {selector}")
                        break

                if not password_field:
                    print("✗ Password field not found. Check mycourses_after_email.png")
                    return

                print("Entering password...")
                password_field.fill(password)

                page.screenshot(path="mycourses_before_submit.png")
                print("Screenshot saved as mycourses_before_submit.png")

                # Click sign in
                print("Clicking sign in button...")
                page.click("#idSIButton9")

                # Wait for MFA approval / "Stay signed in?" prompt
                print("Waiting for MFA authentication...")
                print("⏳ Please approve the sign-in request on your device "
                      "(Microsoft Authenticator / SMS / call)...")

                # Wait longer to allow for MFA approval
                time.sleep(30)

                # Take screenshot after submission
                page.screenshot(path="mycourses_after_submit.png")
                print("Screenshot saved as mycourses_after_submit.png")
                print(f"Current URL: {page.url}")

            except Exception as e:
                print(f"✗ Error during login: {e}")
                page.screenshot(path="mycourses_error.png")
                print("Screenshot saved as mycourses_error.png")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    test_mycourses_access()
