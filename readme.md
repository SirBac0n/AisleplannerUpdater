# Aisle Planner RSVP Auto-Updater

Reads guest RSVP data from a CSV (e.g. exported from the "RSVP Bridge" sheet
you fill in from Zola) and updates each guest's status on Aisle Planner's
RSVP Summary page by clicking their status icon the right number of times.

BEFORE YOU RUN THIS, YOU MUST FILL IN THE "SITE-SPECIFIC SETTINGS" SECTION
BELOW. Every value marked TODO needs to be found using your browser's
inspector (right-click an element on the page -> Inspect). This script
cannot work with placeholder values.

## Setup:
    pip install playwright python-dotenv
    playwright install chromium

    Create a ".env" file in this same folder (see .env.example) with:
        AISLEPLANNER_EMAIL=you@example.com
        AISLEPLANNER_PASSWORD=yourpassword

## Usage:
    python update_rsvp_statuses.py rsvp_data.csv

CSV format expected (see rsvp_bridge_template.xlsx for reference):
    First Name, Last Name, RSVP Status
    (RSVP Status should be one of Zola's own values: "No Response",
    "Attending", "Declined" -- the script translates these into Aisle
    Planner's status cycle via ZOLA_TO_AISLEPLANNER_STATUS below)