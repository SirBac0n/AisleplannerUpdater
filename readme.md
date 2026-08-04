# Aisle Planner RSVP Auto-Updater

Reads guest RSVP data from a CSV (e.g. exported from Zola) and updates 
each guest's status on Aisle Planner's RSVP Summary page by clicking 
their status icon the right number of times.

## Setup:
    pip install playwright python-dotenv
    playwright install chromium

    Create a ".env" file in this same folder (see .env.example) with:
        AISLEPLANNER_EMAIL=you@example.com
        AISLEPLANNER_PASSWORD=yourpassword

## Usage:
    python update_rsvp_statuses.py rsvp_data.csv

CSV format expected:
    First Name, Last Name, RSVP Status
    (RSVP Status should be one of Zola's own values: "No Response",
    "Attending", "Declined" -- the script translates these into Aisle
    Planner's status cycle)