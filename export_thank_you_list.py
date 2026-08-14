"""
Zola Thank-You Letter Exporter
-------------------------------
Pulls two things from Zola using your saved, pre-authenticated session
(see save_zola_session.py) and merges them into one CSV you can mail-merge
into thank-you letters:

  1. The gift list -- who gave what, via Zola's own "Download thank you
     list" export (gift-tracker page). This has the giver's name and the
     gift(s), but Zola leaves the "Gift Giver Address" column blank for
     anything bought through an external retailer (Amazon, Target, etc.)
     -- it only ships from Zola's own registry checkout would that column
     be filled in, which in practice is rare.
  2. The mailing addresses, via the same API that powers Zola's guest
     list / "Address envelopes" page (web-api/v1/guestgroup/list/all).
     Each guest *household* there has a pre-formatted 4-line envelope
     address block.

Gift givers are matched to guest households by name. Zola has no formal
link between "who gave this gift" and "which guest list entry is that,"
so this is name matching, not a join on IDs -- nicknames ("Grandma
Harmon"), missing middle names, etc. will fail to match and need manual
lookup. Unmatched givers are still included in the output with blank
address fields so nothing gets silently dropped.

Setup:
    pip install playwright python-dotenv
    playwright install chromium
    Run save_zola_session.py once first (see that file / readme.md).

Usage:
    python export_thank_you_list.py
    python export_thank_you_list.py --out my_thank_you_list.csv
"""

import csv
import os
import re
import sys
from collections import defaultdict

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

ZOLA_AUTH_STATE_PATH = "zola_auth_state.json"
ZOLA_BASE_URL = "https://www.zola.com"

# The gift-tracker page the export link lives on -- visited first so the
# export request's Referer matches what Zola expects (same reasoning as
# the RSVP CSV export in update_rsvp_statuses.py).
GIFT_TRACKER_PAGE_URL = f"{ZOLA_BASE_URL}/registry/gift-tracker/your-gifts"
THANK_YOU_CSV_EXPORT_PATH = "/web-registry-api/v1/giftTracker/thanksNotesCsv"
THANK_YOU_CSV_DOWNLOAD_PATH = "zola_thank_you_gifts_export.csv"

GUEST_LIST_PAGE_URL = f"{ZOLA_BASE_URL}/wedding/manage/guests/all"
GUEST_GROUP_API_PATH = "/web-api/v1/guestgroup/list/all"

DEFAULT_OUTPUT_PATH = "thank_you_letters.csv"


def is_logged_into_zola(page: Page) -> bool:
    cookies = page.context.cookies()
    return any(c["name"] == "zolaLoggedIn" and c["value"] == "true" for c in cookies)


def download_thank_you_csv(page: Page) -> str:
    """Downloads Zola's gift/thank-you CSV export, same document-navigation
    trick as download_zola_rsvp_csv() in update_rsvp_statuses.py -- Zola
    rejects a background fetch for this endpoint even with valid cookies."""
    page.goto(GIFT_TRACKER_PAGE_URL)
    page.wait_for_load_state("networkidle")

    export_url = ZOLA_BASE_URL + THANK_YOU_CSV_EXPORT_PATH
    try:
        with page.expect_download(timeout=15000) as download_info:
            try:
                page.goto(export_url)
            except Exception:
                pass  # navigation aborted because a real download started
        download_info.value.save_as(THANK_YOU_CSV_DOWNLOAD_PATH)
    except PWTimeout:
        response = page.goto(export_url)
        if response is None or not response.ok:
            status = response.status if response else "no response"
            raise RuntimeError(
                f"Zola thank-you-list export failed (status: {status}). "
                f"A 401 likely means the saved session expired -- run "
                f"save_zola_session.py again."
            )
        with open(THANK_YOU_CSV_DOWNLOAD_PATH, "wb") as f:
            f.write(response.body())

    print(f"Downloaded gift list to {THANK_YOU_CSV_DOWNLOAD_PATH}")
    return THANK_YOU_CSV_DOWNLOAD_PATH


def load_gifts(csv_path: str) -> list:
    """Returns a list of {giver, item, qty, value, thanked} dicts, one per
    gift line item. A single giver may appear on multiple lines."""
    gifts = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            giver = (row.get("Gift Giver") or "").strip()
            if not giver:
                continue
            gifts.append({
                "giver": giver,
                "item": (row.get("Name") or "").strip(),
                "qty": (row.get("Qty") or "").strip(),
                "value": (row.get("Gift Value") or "").strip(),
                "thanked": (row.get("Gift Thank You Note Sent") or "").strip().lower() == "yes",
            })
    return gifts


def fetch_guest_groups(page: Page) -> list:
    """Loads the guest list page and captures the guestgroup/list/all API
    response that powers it -- this is where full mailing addresses live
    (the gift export itself doesn't have them). Returns the raw list of
    guest_group dicts."""
    captured = []

    def on_response(resp):
        if GUEST_GROUP_API_PATH in resp.url and resp.ok:
            try:
                captured.append(resp.json())
            except Exception:
                pass

    page.on("response", on_response)
    page.goto(GUEST_LIST_PAGE_URL)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    page.remove_listener("response", on_response)

    if not captured:
        raise RuntimeError(
            f"Never saw a {GUEST_GROUP_API_PATH} response while loading "
            f"{GUEST_LIST_PAGE_URL} -- Zola may have changed how this page "
            f"loads its data. Check _zola_explore_output-style debugging."
        )

    all_groups = []
    for payload in captured:
        all_groups.extend(payload.get("guest_groups", []))
    return all_groups


def normalize_name(name: str) -> str:
    """Lowercases and strips everything but letters/spaces so trivial
    punctuation differences ("Jessica Oakleigh!)" vs "Jessica Oakleigh")
    don't block a match."""
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def build_guest_index(guest_groups: list) -> tuple:
    """Returns (by_full_name, by_last_name) lookup dicts mapping normalized
    name -> guest_group, built from every individual guest across every
    household. by_last_name maps to a *list* since last names collide."""
    by_full_name = {}
    by_last_name = defaultdict(list)
    for group in guest_groups:
        for guest in group.get("guests", []):
            first = normalize_name(guest.get("first_name") or "")
            last = normalize_name(guest.get("family_name") or "")
            if not first or not last:
                continue
            by_full_name[f"{first} {last}"] = group
            if group not in by_last_name[last]:
                by_last_name[last].append(group)
    return by_full_name, by_last_name


def match_giver_to_group(giver_name: str, by_full_name: dict, by_last_name: dict):
    """Returns (guest_group_or_None, match_quality_str)."""
    normalized = normalize_name(giver_name)
    if not normalized:
        return None, "unmatched"

    if normalized in by_full_name:
        return by_full_name[normalized], "exact"

    # Fall back to last-name-only if it's unambiguous -- handles nicknames
    # ("Grandma Harmon" -> last name "Harmon") but only when there's
    # exactly one Harmon household, otherwise it's a guess we shouldn't make.
    last_word = normalized.split()[-1] if normalized.split() else ""
    candidates = by_last_name.get(last_word, [])
    if len(candidates) == 1:
        return candidates[0], "last-name-only (verify)"

    return None, "unmatched"


def format_address_lines(group: dict) -> list:
    """Returns the 4-line envelope address block Zola already formats for
    this household, e.g. ["Mrs. Sophie and Mr. Luke Balizet",
    "5300 Riverside Drive Unit 162", "Upper Arlington, OH 43220", ""]."""
    addr = group.get("recipient_address") or {}
    return [addr.get("line1", ""), addr.get("line2", ""),
            addr.get("line3", ""), addr.get("line4", "")]


def build_thank_you_rows(gifts: list, guest_groups: list) -> list:
    by_full_name, by_last_name = build_guest_index(guest_groups)

    # Group gift line items by giver name so one household gets one row
    # listing every gift they gave, not a separate row per item.
    by_giver = defaultdict(list)
    for gift in gifts:
        by_giver[gift["giver"]].append(gift)

    rows = []
    for giver, giver_gifts in sorted(by_giver.items()):
        group, match_quality = match_giver_to_group(giver, by_full_name, by_last_name)
        address_lines = format_address_lines(group) if group else ["", "", "", ""]

        items_summary = "; ".join(
            f"{g['item']} (x{g['qty']})" if g["qty"] not in ("", "1") else g["item"]
            for g in giver_gifts
        )
        all_thanked = all(g["thanked"] for g in giver_gifts)

        rows.append({
            "Gift Giver (as entered in Zola)": giver,
            "Match Status": match_quality,
            "Address Line 1": address_lines[0],
            "Address Line 2": address_lines[1],
            "Address Line 3": address_lines[2],
            "Address Line 4": address_lines[3],
            "Gifts": items_summary,
            "Already Thanked": "Yes" if all_thanked else "No",
        })
    return rows


def write_output_csv(rows: list, out_path: str):
    fieldnames = [
        "Gift Giver (as entered in Zola)", "Match Status",
        "Address Line 1", "Address Line 2", "Address Line 3", "Address Line 4",
        "Gifts", "Already Thanked",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    out_path = DEFAULT_OUTPUT_PATH
    if "--out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--out") + 1]

    if not os.path.exists(ZOLA_AUTH_STATE_PATH):
        print(
            f"No saved Zola session found at {ZOLA_AUTH_STATE_PATH}.\n"
            f"Run save_zola_session.py once, by hand, to log in first."
        )
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True, storage_state=ZOLA_AUTH_STATE_PATH,
        )
        page = context.new_page()
        try:
            if not is_logged_into_zola(page):
                raise RuntimeError(
                    f"Not logged into Zola. The saved session in "
                    f"{ZOLA_AUTH_STATE_PATH} is missing, invalid, or "
                    f"expired. Run save_zola_session.py again."
                )

            gifts_csv_path = download_thank_you_csv(page)
            gifts = load_gifts(gifts_csv_path)
            print(f"Loaded {len(gifts)} gift line item(s) from {len(set(g['giver'] for g in gifts))} giver(s).")

            print("Fetching guest list addresses...")
            guest_groups = fetch_guest_groups(page)
            print(f"Loaded {len(guest_groups)} guest household(s) from the guest list.")

            rows = build_thank_you_rows(gifts, guest_groups)
            write_output_csv(rows, out_path)

            matched = sum(1 for r in rows if r["Match Status"] != "unmatched")
            verify = sum(1 for r in rows if r["Match Status"] == "last-name-only (verify)")
            unmatched = sum(1 for r in rows if r["Match Status"] == "unmatched")
            print(f"\nWrote {len(rows)} giver row(s) to {out_path}")
            print(f"  {matched - verify} matched exactly")
            print(f"  {verify} matched by last name only -- double-check these")
            print(f"  {unmatched} unmatched -- no address found, fill in by hand:")
            if unmatched:
                for r in rows:
                    if r["Match Status"] == "unmatched":
                        print(f"    - {r['Gift Giver (as entered in Zola)']}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
