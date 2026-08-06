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
# API endpoint (found via inspector). Since it's just a GET request, once
# we're logged in (and the browser context holds the session cookie) we
# can fetch it directly instead of clicking through the UI -- faster and
# less fragile than waiting on a dropdown to render.
ZOLA_BASE_URL = "https://www.zola.com"
ZOLA_CSV_EXPORT_PATH = "/web-api/v1/guestgroup/export/rsvp-overview"

# Where the downloaded file should be saved locally
ZOLA_DOWNLOAD_PATH = "zola_rsvp_export.csv"

import os
from dotenv import load_dotenv

load_dotenv()
ZOLA_EMAIL = os.environ.get("ZOLA_EMAIL")
ZOLA_PASSWORD = os.environ.get("ZOLA_PASSWORD")
EMAIL = os.environ.get("AISLEPLANNER_EMAIL")
PASSWORD = os.environ.get("AISLEPLANNER_PASSWORD")


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


def download_zola_rsvp_csv(page: Page) -> str:
    """
    Logs into Zola and downloads the guest/RSVP export CSV.
    Returns the local path of the downloaded file.
    """
    if not ZOLA_EMAIL or not ZOLA_PASSWORD:
        raise RuntimeError(
            "Missing Zola credentials. Add ZOLA_EMAIL and ZOLA_PASSWORD to "
            "your .env file (see .env.example)."
        )
    if "TODO" in ZOLA_GUEST_LIST_URL or "TODO" in ZOLA_LOGIN_URL:
        raise RuntimeError(
            "ZOLA_GUEST_LIST_URL and/or ZOLA_LOGIN_URL still contain a "
            "placeholder value. Log into Zola manually, copy the real URLs "
            "from your browser's address bar, and update the SITE-SPECIFIC "
            "SETTINGS section at the top of this script."
        )
 
    page.goto(ZOLA_LOGIN_URL)
    page.wait_for_selector("input[type='email']", timeout=15000)
    page.fill("input[type='email']", ZOLA_EMAIL)
    page.fill("input[type='password']", ZOLA_PASSWORD)
 
    # Small buffer so the form's own validation (often triggered on
    # blur/change) has time to run before we click -- clicking too fast
    # can occasionally beat that validation and the click does nothing.
    page.wait_for_timeout(500)
 
    login_attempts = 0
    while True:
        login_attempts += 1
        page.click("button.LOGIN-submit")
        # Wait for the URL to actually change away from the login page,
        # rather than trusting networkidle -- SPAs often keep background
        # connections open, making networkidle fire inconsistently.
        try:
            page.wait_for_url(lambda url: url != ZOLA_LOGIN_URL, timeout=10000)
        except PWTimeout:
            pass
 
        if page.url.rstrip("/") == ZOLA_BASE_URL.rstrip("/"):
            # Landed on the homepage instead of being logged in -- login
            # didn't take. Retry once before giving up.
            if login_attempts >= 3:
                raise RuntimeError(
                    "Zola login redirected to the homepage 3 times in a row. "
                    "Check for a CAPTCHA, 2FA prompt, or incorrect credentials "
                    "by watching the browser window during a manual run."
                )
            print(f"  Login attempt {login_attempts} redirected to homepage, retrying...")
            page.goto(ZOLA_LOGIN_URL)
            page.wait_for_selector("input[type='email']", timeout=15000)
            page.fill("input[type='email']", ZOLA_EMAIL)
            page.fill("input[type='password']", ZOLA_PASSWORD)
            page.wait_for_timeout(500)
            continue
        break
 
    page.goto(ZOLA_GUEST_LIST_URL)
    page.wait_for_load_state("networkidle")
 
    # Fetch the CSV export directly via the API endpoint rather than
    # clicking through the export button/dropdown. This reuses the
    # logged-in browser context's session cookies automatically.
    export_url = ZOLA_BASE_URL + ZOLA_CSV_EXPORT_PATH
    response = page.request.get(export_url)
    if not response.ok:
        raise RuntimeError(
            f"Zola export request failed: {response.status} {response.status_text}. "
            f"Double check ZOLA_CSV_EXPORT_PATH is still correct, or fall back to "
            f"clicking the Export RSVPs button manually."
        )
 
    with open(ZOLA_DOWNLOAD_PATH, "wb") as f:
        f.write(response.body())
 
    print(f"Downloaded Zola RSVP export to {ZOLA_DOWNLOAD_PATH}")
    return ZOLA_DOWNLOAD_PATH


def is_logged_into_zola(page: Page) -> bool:
    """Checks for the zolaLoggedIn=true cookie, which only gets set on a
    genuinely authenticated session -- more reliable than inferring
    success from the URL, which can look fine even when auth silently
    failed."""
    cookies = page.context.cookies()
    return any(c["name"] == "zolaLoggedIn" and c["value"] == "true" for c in cookies)


def download_zola_rsvp_csv(page: Page) -> str:
    """
    Logs into Zola and downloads the guest/RSVP export CSV.
    Returns the local path of the downloaded file.
    """
    if not ZOLA_EMAIL or not ZOLA_PASSWORD:
        raise RuntimeError(
            "Missing Zola credentials. Add ZOLA_EMAIL and ZOLA_PASSWORD to "
            "your .env file (see .env.example)."
        )
    if "TODO" in ZOLA_GUEST_LIST_URL or "TODO" in ZOLA_LOGIN_URL:
        raise RuntimeError(
            "ZOLA_GUEST_LIST_URL and/or ZOLA_LOGIN_URL still contain a "
            "placeholder value. Log into Zola manually, copy the real URLs "
            "from your browser's address bar, and update the SITE-SPECIFIC "
            "SETTINGS section at the top of this script."
        )
 
    login_attempts = 0
    while True:
        login_attempts += 1
        page.goto(ZOLA_LOGIN_URL)
        page.wait_for_selector("input[type='email']", timeout=15000)
        page.fill("input[type='email']", ZOLA_EMAIL)
        page.fill("input[type='password']", ZOLA_PASSWORD)
 
        # Small buffer so the form's own validation (often triggered on
        # blur/change) has time to run before we click -- clicking too fast
        # can occasionally beat that validation and the click does nothing.
        page.wait_for_timeout(500)
 
        page.click("button.LOGIN-submit")
        # Wait for the URL to change, mostly as a sign the click actually
        # did something -- but this is NOT what determines success below.
        try:
            page.wait_for_url(lambda url: url != ZOLA_LOGIN_URL, timeout=10000)
        except PWTimeout:
            pass
        # Give the zolaLoggedIn cookie a moment to actually get set after
        # the redirect completes.
        page.wait_for_timeout(1000)
 
        if is_logged_into_zola(page):
            break
 
        debug_path = f"zola_login_failure_attempt{login_attempts}.png"
        page.screenshot(path=debug_path)
        print(f"  Login attempt {login_attempts} did not result in an "
              f"authenticated session (no zolaLoggedIn cookie). Landed on "
              f"{page.url}. Screenshot saved to {debug_path}.")
 
        if login_attempts >= 3:
            raise RuntimeError(
                "Zola login failed 3 times in a row -- the zolaLoggedIn "
                "cookie never got set. Check the saved screenshots for a "
                "CAPTCHA, 2FA prompt, 'suspicious activity' warning, or "
                "incorrect credentials. Headless mode is more likely to "
                "trigger anti-bot checks than a visible browser."
            )
 
    page.goto(ZOLA_GUEST_LIST_URL)
    page.wait_for_load_state("networkidle")
 
 
    # Fetch the CSV export directly via the API endpoint rather than
    # clicking through the export button/dropdown. This reuses the
    # logged-in browser context's session cookies automatically.
    # The ".csv format" link in Zola's export dropdown points directly at
    # this API endpoint (found via inspector). We navigate to it directly
    # rather than clicking through the dropdown UI -- but importantly,
    # this must be a real page navigation (page.goto), not a background
    # API call. Zola's server checks for Sec-Fetch-Dest: document (i.e.
    # "this looks like a real browser navigating here"), which a raw
    # page.request.get() call doesn't send, and rejects it with a 401
    # even with valid session cookies attached.
    export_url = ZOLA_BASE_URL + ZOLA_CSV_EXPORT_PATH
    response = page.goto(export_url)
 
    if response is None or not response.ok:
        debug_path = "zola_debug_screenshot.png"
        page.screenshot(path=debug_path)
        status = response.status if response else "no response"
        raise RuntimeError(
            f"Zola export request failed (status: {status}). "
            f"Current page URL was: {page.url}. "
            f"Saved a screenshot to {debug_path} for debugging."
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
    # TODO: confirm these field selectors match the actual login form
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
 
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
 
        if csv_path is None:
            # Use a separate browser context (isolated cookies/session) for
            # Zola so its login doesn't interfere with Aisle Planner's.
            zola_context = browser.new_context(accept_downloads=True)
            zola_page = zola_context.new_page()
            try:
                csv_path = download_zola_rsvp_csv(zola_page)
            except PWTimeout:
                print("Timed out on Zola -- most likely the download never "
                      "fired (check ZOLA_GUEST_LIST_URL matches the exact "
                      "page the export link lives on, so the Referer header "
                      "matches what Zola expects), or ZOLA_LOGIN_URL is wrong.")
                browser.close()
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
            browser.close()


if __name__ == "__main__":
    main()