# Aisle Planner RSVP Auto-Updater

Reads guest RSVP data from a CSV (e.g. exported from Zola) and updates 
each guest's status on Aisle Planner's RSVP Summary page by clicking 
their status icon the right number of times.

Be sure to run save_zola_session.py first and properly set up the
environment and variables before running update_rsvp_statuses.py to
ensure the script works and everything gets updated properly

## save_zola_session.py:

Zola sends an email verification code to any browser/device it doesn't
recognize. update_rsvp_statuses.py has no way to read your email or type
that code in, so it can't complete Zola's login on its own.

save_zola_session.py opens a REAL, VISIBLE browser window and pauses so YOU can:
  1. Enter your email and password
  2. Check your email for Zola's verification code
  3. Type that code into the browser window yourself

Once you're fully logged in, it saves the authenticated session (cookies
etc.) to zola_auth_state.json. update_rsvp_statuses.py then reuses that
file on every future run instead of logging in fresh -- so the email
challenge only ever happens this one time.

### When to re-run this:
  - The first time you set things up
  - Whenever update_rsvp_statuses.py fails with a "session expired" or
    401 error -- sessions don't last forever, and Zola may eventually
    ask for verification again even for a previously-trusted browser

### Setup:
    pip install playwright python-dotenv
    playwright install chromium

### Usage:
    python save_zola_session.py

## update_rsvp_statuses.py:

### Setup:
    pip install playwright python-dotenv
    playwright install chromium
 
    Create a ".env" file in this same folder (see .env.example) with:
        ZOLA_EMAIL=you@example.com
        ZOLA_PASSWORD=yourpassword
        AISLEPLANNER_EMAIL=you@example.com
        AISLEPLANNER_PASSWORD=yourpassword
 
### Usage:
  - Fully automated: logs into Zola, downloads the RSVP CSV, then
    updates Aisle Planner with it.
    python update_rsvp_statuses.py
 
  - Or skip the Zola download and use a CSV you already have:
    python update_rsvp_statuses.py --csv rsvp_data.csv

CSV format expected:
    First Name, Last Name, RSVP Status
    (RSVP Status should be one of Zola's own values: "No Response",
    "Attending", "Declined" -- the script translates these into Aisle
    Planner's status cycle)