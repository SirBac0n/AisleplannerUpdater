"""
Aisle Planner RSVP Auto-Updater
--------------------------------
Reads guest RSVP data from a CSV (e.g. exported from Zola) and updates 
each guest's status on Aisle Planner's RSVP Summary page by clicking 
their status icon the right number of times.
 
BEFORE YOU RUN THIS, YOU MUST FILL IN THE "SITE-SPECIFIC SETTINGS" SECTION
BELOW.
 
Setup:
    pip install playwright python-dotenv
    playwright install chromium
 
    Create a ".env" file in this same folder (see .env.example) with:
        ZOLA_EMAIL=you@example.com
        ZOLA_PASSWORD=yourpassword
        AISLEPLANNER_EMAIL=you@example.com
        AISLEPLANNER_PASSWORD=yourpassword

One-time setup for Zola's email verification step (see save_zola_session.py):
    Zola sends an email verification code to any browser/device it doesn't
    recognize, which this script can't read or enter on its own. Run
    save_zola_session.py ONCE, by hand, to complete that verification and
    save the resulting authenticated session to zola_auth_state.json. This
    script then reuses that saved session on every run instead of logging
    in fresh, so the email challenge only ever happens that one time (until
    the saved session eventually expires -- see save_zola_session.py).
 
Usage:
    # Fully automated: logs into Zola, downloads the RSVP CSV, then
    # updates Aisle Planner with it.
    python update_rsvp_statuses.py
 
    # Or skip the Zola download and use a CSV you already have:
    python update_rsvp_statuses.py --csv rsvp_data.csv
 
CSV format expected (see rsvp_bridge_template.xlsx for reference):
    First Name, Last Name, RSVP Status
    (RSVP Status should be one of Zola's own values: "No Response",
    "Attending", "Declined" -- the script translates these into Aisle
    Planner's status cycle via ZOLA_TO_AISLEPLANNER_STATUS below)
"""

import csv
import sys
import time
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# =====================================================================
# SITE-SPECIFIC SETTINGS -- fill these in using your browser's inspector
# =====================================================================

LOGIN_URL = "https://www.aisleplanner.com/signin"
RSVP_PAGE_URL = "https://www.aisleplanner.com/app/project/573193/tools/guests/summary"

# The order the status icon cycles through on each click, on the
# AISLE PLANNER side.
STATUS_CYCLE = ["Invited", "Attending", "Declined", "Not Invited"]

# Zola's RSVP export uses a different, smaller set of statuses than Aisle
# Planner. This maps each Zola status to the Aisle Planner status it should
# become. Adjust the right-hand side if you'd rather map differently
# (e.g. "No Response" -> "Waitlisted" instead of "Invited").
ZOLA_TO_AISLEPLANNER_STATUS = {
    "No Response": "Invited",
    "Attending": "Attending",
    "Declined": "Declined",
}

# CSS selector for a single guest row in the RSVP table.
# Right-click a guest row -> Inspect -> find the containing <tr> or <div>
# and grab a class name that's shared by all guest rows.
GUEST_ROW_SELECTOR = "tbody tr"

# Column positions within a row (1-indexed, matches nth-child):
#   1 = RSVP # / action column ("plaintext action")
#   2 = Last Name  (<td class="input"><input type="text"></td>)
#   3 = First Name (<td class="input"><input type="text"></td>)
#   4 = Ceremony RSVP status (<td class="response main"><div class="title" title="..."></div></td>)
#   5 = Reception RSVP status (same structure as Ceremony)
LAST_NAME_SELECTOR = "td:nth-child(2) input"
FIRST_NAME_SELECTOR = "td:nth-child(3) input"
CEREMONY_STATUS_SELECTOR = "td.response.main:nth-child(4) div[title]"
RECEPTION_STATUS_SELECTOR = "td.response.main:nth-child(5) div[title]"

# Which status column(s) to update. Options: "ceremony", "reception", "both"
# set this based on whether your Zola export tracks Ceremony/Reception
# separately or as one combined RSVP.
STATUS_COLUMNS_TO_UPDATE = "both"

# =====================================================================
# ZOLA SETTINGS -- fill these in the same way, using Zola's own pages
# =====================================================================
 
ZOLA_LOGIN_URL = "https://www.zola.com/account/login"

# URL of your guest list / RSVP manager page on Zola once logged in
ZOLA_GUEST_LIST_URL = "https://www.zola.com/wedding/manage/guests/rsvps/overview"

# The ".csv format" link in Zola's export dropdown points directly at this
# API endpoint (found via inspector). We navigate to it directly (via a
# real page.goto, not a background fetch -- Zola's server checks for
# Sec-Fetch-Dest: document and rejects anything that doesn't look like a
# genuine browser navigation, even with valid session cookies attached).
ZOLA_BASE_URL = "https://www.zola.com"
ZOLA_CSV_EXPORT_PATH = "/web-api/v1/guestgroup/export/rsvp-overview"

# Where the downloaded file should be saved locally
ZOLA_DOWNLOAD_PATH = "zola_rsvp_export.csv"

# Where the saved, pre-authenticated Zola session lives (see
# save_zola_session.py). This file lets the script skip Zola's login form
# -- and therefore skip the email verification challenge -- entirely.
ZOLA_AUTH_STATE_PATH = "zola_auth_state.json"

import os
from dotenv import load_dotenv

load_dotenv()
ZOLA_EMAIL = os.environ.get("ZOLA_EMAIL")
ZOLA_PASSWORD = os.environ.get("ZOLA_PASSWORD")
EMAIL = os.environ.get("AISLEPLANNER_EMAIL")
PASSWORD = os.environ.get("AISLEPLANNER_PASSWORD")

# How much to slow down each Playwright action, in milliseconds. Higher
# values make it easier to watch what's happening in a visible browser;
# 0 (the default) is fastest for unattended runs (CI). Override by setting
# the SLOW_MO_MS environment variable, e.g. SLOW_MO_MS=200 for local
# debugging with a visible browser.
SLOW_MO_MS = int(os.environ.get("SLOW_MO_MS", "0"))


def get_current_status(status_div) -> str:
    """
    Reads the guest row's status from the div's `title` attribute, e.g.
    <div class="title" title="Invited"></div> -> "Invited"
    """
    status = status_div.get_attribute("title")
    if status not in STATUS_CYCLE:
        raise ValueError(f"title attribute '{status}' isn't in STATUS_CYCLE")
    return status


def clicks_needed(current: str, target: str) -> int:
    """How many clicks to cycle from current status to target status."""
    if current == target:
        return 0
    start = STATUS_CYCLE.index(current)
    end = STATUS_CYCLE.index(target)
    return (end - start) % len(STATUS_CYCLE)


def load_guest_updates(csv_path: str) -> dict:
    """
    Returns {full_name: target_status} from the CSV, translating each
    Zola status into its Aisle Planner equivalent via ZOLA_TO_AISLEPLANNER_STATUS.
    """
    updates = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = f"{row['First Name'].strip()} {row['Last Name'].strip()}"
            zola_status = row["The Wedding"].strip()

            target_status = ZOLA_TO_AISLEPLANNER_STATUS.get(zola_status)
            if target_status is None:
                print(f"  [skip] {name}: unrecognized Zola status '{zola_status}' "
                      f"(add it to ZOLA_TO_AISLEPLANNER_STATUS if this is valid)")
                continue
            if target_status not in STATUS_CYCLE:
                print(f"  [skip] {name}: mapped status '{target_status}' isn't in "
                      f"STATUS_CYCLE -- check ZOLA_TO_AISLEPLANNER_STATUS")
                continue

            updates[name] = target_status
    return updates


def is_logged_into_zola(page: Page) -> bool:
    """Checks for the zolaLoggedIn=true cookie, which only gets set on a
    genuinely authenticated session -- more reliable than inferring
    success from the URL, which can look fine even when auth silently
    failed."""
    cookies = page.context.cookies()
    return any(c["name"] == "zolaLoggedIn" and c["value"] == "true" for c in cookies)


def download_zola_rsvp_csv(page: Page) -> str:
    """
    Downloads the guest/RSVP export CSV from Zola. Assumes the page's
    browser context was already created with a saved, pre-authenticated
    session (see ZOLA_AUTH_STATE_PATH / save_zola_session.py) -- this
    function no longer logs in itself, since doing so from scratch would
    trigger Zola's email verification challenge, which this script can't
    complete on its own.
    Returns the local path of the downloaded file.
    """
    if not is_logged_into_zola(page):
        raise RuntimeError(
            f"Not logged into Zola. The saved session in "
            f"{ZOLA_AUTH_STATE_PATH} is missing, invalid, or expired. Run "
            f"save_zola_session.py again to re-authenticate (you'll need "
            f"to enter the emailed verification code once)."
        )

    page.goto(ZOLA_GUEST_LIST_URL)
    page.wait_for_load_state("networkidle")

    # Fetch the CSV export via a real page navigation (not a background
    # API call) -- Zola's server checks for Sec-Fetch-Dest: document and
    # rejects a raw page.request.get() with a 401 even with valid session
    # cookies attached.
    export_url = ZOLA_BASE_URL + ZOLA_CSV_EXPORT_PATH
    response = page.goto(export_url)

    if response is None or not response.ok:
        debug_path = "zola_debug_screenshot.png"
        page.screenshot(path=debug_path)
        status = response.status if response else "no response"
        raise RuntimeError(
            f"Zola export request failed (status: {status}). "
            f"Current page URL was: {page.url}. "
            f"Saved a screenshot to {debug_path} for debugging. "
            f"A 401 here likely means the saved session has expired -- "
            f"run save_zola_session.py again."
        )

    # The server returns the CSV directly with no Content-Disposition:
    # attachment header, so Chrome renders it inline as a page instead of
    # triggering a "download" -- we grab the response body straight from
    # the navigation instead of waiting for a download event that never
    # fires.
    with open(ZOLA_DOWNLOAD_PATH, "wb") as f:
        f.write(response.body())

    print(f"Downloaded Zola RSVP export to {ZOLA_DOWNLOAD_PATH}")
    return ZOLA_DOWNLOAD_PATH


def login(page: Page):
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Missing credentials. Create a .env file in this folder with "
            "AISLEPLANNER_EMAIL and AISLEPLANNER_PASSWORD set (see .env.example)."
        )
    page.goto(LOGIN_URL)
    page.fill("input[type='text']", EMAIL)
    page.fill("input[type='password']", PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


def update_rsvps(page: Page, updates: dict):
    page.goto(RSVP_PAGE_URL)
    page.wait_for_selector(GUEST_ROW_SELECTOR, timeout=15000)
 
    rows = page.query_selector_all(GUEST_ROW_SELECTOR)
    print(f"Found {len(rows)} guest rows on the page.")
 
    columns_to_update = []
    if STATUS_COLUMNS_TO_UPDATE in ("ceremony", "both"):
        columns_to_update.append(("Ceremony", CEREMONY_STATUS_SELECTOR))
    if STATUS_COLUMNS_TO_UPDATE in ("reception", "both"):
        columns_to_update.append(("Reception", RECEPTION_STATUS_SELECTOR))
 
    matched_names = set()
    for row in rows:
        last_input = row.query_selector(LAST_NAME_SELECTOR)
        first_input = row.query_selector(FIRST_NAME_SELECTOR)
        if not last_input or not first_input:
            continue
 
        last_name = (last_input.input_value() or "").strip()
        first_name = (first_input.input_value() or "").strip()
        name = f"{first_name} {last_name}".strip()
 
        if not name or name not in updates:
            continue
 
        matched_names.add(name)
        target = updates[name]
 
        for column_label, selector in columns_to_update:
            status_div = row.query_selector(selector)
            if not status_div:
                print(f"  [error] {name}: couldn't find {column_label} status element")
                continue
            try:
                current = get_current_status(status_div)
            except ValueError as e:
                print(f"  [error] {name} ({column_label}): {e}")
                continue
 
            n_clicks = clicks_needed(current, target)
            if n_clicks == 0:
                print(f"  [ok] {name} ({column_label}) already '{target}'")
                continue
 
            print(f"  [update] {name} ({column_label}): {current} -> {target} ({n_clicks} click(s))")
            for _ in range(n_clicks):
                status_div.click()
                time.sleep(0.5)  # let the UI update between clicks
 
    print(f"\nMatched {len(matched_names)} of {len(updates)} guests from the CSV.")
    unmatched = set(updates) - matched_names
    if unmatched:
        print("Guests in CSV but not found on page (name mismatch?):")
        for name in unmatched:
            print(f"  - {name}")


def main():
    csv_path = None
    if "--csv" in sys.argv:
        csv_path = sys.argv[sys.argv.index("--csv") + 1]

    if csv_path is None and not os.path.exists(ZOLA_AUTH_STATE_PATH):
        print(
            f"No saved Zola session found at {ZOLA_AUTH_STATE_PATH}.\n"
            f"Run save_zola_session.py once, by hand, to log in and "
            f"complete Zola's email verification step -- this script "
            f"can't do that part on its own.\n"
            f"Alternatively, run with --csv to skip Zola entirely and "
            f"use a CSV you already have."
        )
        sys.exit(1)
 
    with sync_playwright() as p:
        # headless=True since the saved Zola session (from
        # save_zola_session.py) means this script never touches the login
        # form -- the main trigger for Zola's bot detection is gone. If
        # you start seeing 401s again despite a valid saved session, that
        # may mean Zola is validating the session cookie against browser
        # fingerprint consistency; switch back to headless=False (and run
        # under Xvfb in CI) as a fallback in that case.
        browser = p.chromium.launch(headless=True, slow_mo=SLOW_MO_MS)
 
        try:
            if csv_path is None:
                # Load the pre-authenticated session saved by
                # save_zola_session.py instead of logging in fresh --
                # logging in fresh would trigger Zola's email verification
                # challenge, which this script can't complete on its own.
                zola_context = browser.new_context(
                    accept_downloads=True,
                    storage_state=ZOLA_AUTH_STATE_PATH,
                )
                zola_page = zola_context.new_page()
                try:
                    csv_path = download_zola_rsvp_csv(zola_page)
                except PWTimeout:
                    print("Timed out on Zola -- most likely the download never "
                          "fired (check ZOLA_GUEST_LIST_URL matches the exact "
                          "page the export link lives on, so the Referer header "
                          "matches what Zola expects).")
                    return
                finally:
                    zola_context.close()
 
            updates = load_guest_updates(csv_path)
            print(f"Loaded {len(updates)} target statuses from {csv_path}")
 
            ap_context = browser.new_context()
            page = ap_context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})
            try:
                login(page)
                update_rsvps(page, updates)
            except PWTimeout:
                print("Timed out waiting for a page element -- check your "
                      "selectors in the SITE-SPECIFIC SETTINGS section.")
            finally:
                # Skip the "press Enter to close" prompt when running
                # unattended (cron, GitHub Actions, etc.) -- there's no one
                # there to press it, and it would just hang forever.
                if sys.stdin.isatty():
                    input("\nDone. Press Enter to close the browser...")
        finally:
            # Always close the browser, even if a RuntimeError propagates
            # past both try blocks above -- otherwise an orphaned Chromium
            # process can hang around and delay the job from finishing.
            browser.close()


if __name__ == "__main__":
    main()