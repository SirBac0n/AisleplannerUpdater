"""
One-time Zola session setup
-----------------------------
Zola sends an email verification code to any browser/device it doesn't
recognize. update_rsvp_statuses.py has no way to read your email or type
that code in, so it can't complete Zola's login on its own.

This script opens a REAL, VISIBLE browser window and pauses so YOU can:
  1. Enter your email and password
  2. Check your email for Zola's verification code
  3. Type that code into the browser window yourself

Once you're fully logged in, it saves the authenticated session (cookies
etc.) to zola_auth_state.json. update_rsvp_statuses.py then reuses that
file on every future run instead of logging in fresh -- so the email
challenge only ever happens this one time.

When to re-run this:
  - The first time you set things up
  - Whenever update_rsvp_statuses.py fails with a "session expired" or
    401 error -- sessions don't last forever, and Zola may eventually
    ask for verification again even for a previously-trusted browser

Setup:
    pip install playwright python-dotenv
    playwright install chromium

Usage:
    python save_zola_session.py
"""

import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
ZOLA_LOGIN_URL = "https://www.zola.com/account/login"
ZOLA_AUTH_STATE_PATH = "zola_auth_state.json"
ZOLA_EMAIL = os.environ.get("ZOLA_EMAIL")
ZOLA_PASSWORD = os.environ.get("ZOLA_PASSWORD")


def is_logged_into_zola(page) -> bool:
    cookies = page.context.cookies()
    return any(c["name"] == "zolaLoggedIn" and c["value"] == "true" for c in cookies)


def main():
    if not ZOLA_EMAIL or not ZOLA_PASSWORD:
        print("Set ZOLA_EMAIL and ZOLA_PASSWORD in your .env file first.")
        return

    with sync_playwright() as p:
        # Always headed and never automated past the credentials fields --
        # the whole point of this script is that a human completes the
        # verification step.
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.goto(ZOLA_LOGIN_URL)
        print("\nBrowser window opened. Log in manually:")
        print("  1. Enter your email and password")
        print("  2. Click Log in")
        print("  3. If asked for a verification code, check your email and")
        print("     enter it in the browser window")
        print("  4. Once you see your Zola dashboard/guest list, come back here\n")

        # Pre-fill the credentials as a convenience, but don't click
        # anything -- leave every actual step to the person at the
        # keyboard, including submitting the form.
        try:
            page.wait_for_selector("input[type='email']", timeout=15000)
            page.fill("input[type='email']", ZOLA_EMAIL)
            page.fill("input[type='password']", ZOLA_PASSWORD)
        except Exception:
            print("(Could not pre-fill the login form -- just fill it in "
                  "yourself, that's fine.)")

        input("Press Enter here once you're fully logged in (dashboard/guest "
              "list visible)... ")

        if not is_logged_into_zola(page):
            print("\nDoesn't look like you're logged in yet (no zolaLoggedIn "
                  "cookie found). Make sure you're past the verification "
                  "step and see your actual Zola dashboard, then try again.")
            browser.close()
            return

        context.storage_state(path=ZOLA_AUTH_STATE_PATH)
        print(f"\nSaved authenticated session to {ZOLA_AUTH_STATE_PATH}.")
        print("update_rsvp_statuses.py will now reuse this instead of "
              "logging in fresh.")
        browser.close()


if __name__ == "__main__":
    main()