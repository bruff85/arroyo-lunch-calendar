#!/usr/bin/env python3
"""
Tustin USD - Arroyo Elementary Lunch Calendar Generator

Uses the HealthePro API to fetch the monthly lunch menu and generates
a subscribable ICS calendar file.

API endpoints:
  GET /api/organizations/547/sites/4782/menus/ (to discover the active menu and its published months)
  GET /api/organizations/547/menus/<menu_id>/year/YYYY/month/M/date_overwrites

The school publishes a new menu (with a new id) each school year, so the
menu id is discovered from the site's menu list at runtime; MENU_ID below
is only a fallback.

Schedule:
  - Runs on the 27th of each month at 8:15pm PT
  - Retries daily at 10am and 6pm PT until next month is found
  - Stops retrying once next month is successfully loaded
  - Manual triggers always run regardless of date
"""

import hashlib
import json
import uuid
import re
import requests
from datetime import datetime, date, timedelta
import os
from notify import notify_success, notify_found_failure, notify_not_found
from ics_text import description

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BASE_URL        = "https://menus.healthepro.com/api"
ORG_ID          = "547"
SITE_ID         = "4782"
# Last known menu id (2026-27 school year) — used only as a fallback when
# the site's menu list can't identify the right menu for the target month.
MENU_ID         = "137467"
OUTPUT_ICS      = "docs/lunch.ics"

# Parent-facing lunch menu page — the link that goes in every event's notes.
# Built per-run from the menu id select_menu() resolves, NOT hardcoded: the id
# changes every month, so a fixed URL would keep pointing at an old month's
# menu while the events moved on. Same host as BASE_URL, without the /api.
SITE_BASE_URL   = "https://menus.healthepro.com"


def published_menu_url(menu_id):
    """Public page for one month's lunch menu, or "" if we have no id."""
    if not menu_id:
        return ""
    return f"{SITE_BASE_URL}/organizations/{ORG_ID}/sites/{SITE_ID}/menus/{menu_id}"
NEXT_MONTH_FOUND_FILE = "next_month_found.txt"

# Categories to INCLUDE in the event title (entrees only)
ENTREE_CATEGORIES = {"Lunch Entree", "Entree"}

# Rolling window: keep this many months in the ICS file
MONTHS_TO_KEEP  = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LunchCalendarBot/1.0)",
    "Accept": "application/json",
}


# ─────────────────────────────────────────────
# API FUNCTIONS
# ─────────────────────────────────────────────

def fetch_site_menus():
    """Fetch all menus for our site. Returns a list of menu objects."""
    url = f"{BASE_URL}/organizations/{ORG_ID}/sites/{SITE_ID}/menus/"
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json().get("data", [])


def select_menu(menus, target_month_str):
    """
    Pick the menu to use for the target month. The school publishes a new
    menu (with a new id) each school year, so instead of trusting a fixed
    id, prefer whichever menu actually has the target month published:
      1. the last known menu (MENU_ID), if it has the target month
      2. a lunch-named menu that has the target month
      3. any menu that has the target month
      4. fall back to MENU_ID
    Returns (menu_id, published_months).
    """
    def published(menu):
        return menu.get("published_months") or []

    with_target = [m for m in menus if target_month_str in published(m)]
    known = [m for m in with_target if str(m.get("id")) == MENU_ID]
    lunch = [m for m in with_target if "lunch" in str(m.get("name", "")).lower()]
    for pool in (known, lunch, with_target):
        if pool:
            menu = pool[0]
            return str(menu.get("id")), published(menu)
    for menu in menus:
        if str(menu.get("id")) == MENU_ID:
            return MENU_ID, published(menu)
    return MENU_ID, []


def fetch_date_overwrites(menu_id, year, month):
    """
    Fetch the day-by-day menu data for a given month.
    Returns list of day objects with date and menu items.
    """
    url = f"{BASE_URL}/organizations/{ORG_ID}/menus/{menu_id}/year/{year}/month/{month}/date_overwrites"
    print(f"  Fetching: {url}")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json().get("data", [])


# ─────────────────────────────────────────────
# MENU PARSING
# ─────────────────────────────────────────────

def parse_daily_menu(date_overwrites):
    """
    Parse date_overwrites into a dict of {date_obj: [entree_name, ...]}
    Only includes entree items, skips days off (Spring Break etc.)
    """
    daily = {}

    for day_data in date_overwrites:
        day_str = day_data.get("day")
        if not day_str:
            continue

        try:
            day_date = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Skip weekends
        if day_date.weekday() >= 5:
            continue

        # Parse the setting JSON
        setting_raw = day_data.get("setting", "{}")
        try:
            setting = json.loads(setting_raw)
        except (json.JSONDecodeError, TypeError):
            continue

        # Skip days off (Spring Break, holidays etc.)
        days_off = setting.get("days_off", [])
        if isinstance(days_off, dict) and days_off.get("status") == 1:
            desc = days_off.get("description", "Day Off")
            print(f"  Skipping {day_str}: {desc}")
            continue

        # Extract entree items from current_display
        current_display = setting.get("current_display", [])
        entrees = []
        in_entree_section = False

        for item in current_display:
            item_type = item.get("type")
            item_name = item.get("name", "")

            if item_type == "category":
                # Check if we're entering an entree section
                in_entree_section = item_name in ENTREE_CATEGORIES
            elif item_type == "recipe" and in_entree_section:
                if item_name and item_name not in entrees:
                    entrees.append(item_name)

        if entrees:
            daily[day_date] = entrees

    return daily


# ─────────────────────────────────────────────
# ICS GENERATION
# ─────────────────────────────────────────────

def parse_existing_events(ics_path):
    """Parse existing ICS file and return dict of {date_str: event_block}"""
    if not os.path.exists(ics_path):
        return {}
    with open(ics_path, "r", encoding="utf-8") as f:
        content = f.read()
    events = {}
    raw_events = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", content, re.DOTALL)
    for event in raw_events:
        match = re.search(r"DTSTART[^:]*:(\d{8})", event)
        if match:
            date_str = match.group(1)
            events[date_str] = event.strip()
    return events


def get_window_months(new_month, new_year, num_months=MONTHS_TO_KEEP):
    """Returns set of (month, year) tuples for the rolling window."""
    window = set()
    m, y = new_month, new_year
    for _ in range(num_months):
        window.add((m, y))
        m = 12 if m == 1 else m - 1
        y = y - 1 if m == 12 else y
    return window


def generate_ics(daily_menu, month, year, menu_id, existing_ics_path=None):
    """Generate cumulative ICS with rolling 4-month window."""
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    window = get_window_months(month, year)

    # Load and filter existing events to rolling window
    existing_events = {}
    if existing_ics_path:
        all_existing = parse_existing_events(existing_ics_path)
        for date_str, event_block in all_existing.items():
            try:
                event_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
                if (event_date.month, event_date.year) in window:
                    existing_events[date_str] = event_block
            except ValueError:
                continue
        print(f"  Retaining {len(existing_events)} events within {MONTHS_TO_KEEP}-month window.")

    # Build new events for this month
    new_events = {}
    for day_date in sorted(daily_menu.keys()):
        items = daily_menu[day_date]
        title = " | ".join(items) if items else "Lunch Menu"
        date_str = day_date.strftime("%Y%m%d")
        uid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tustin-lunch-{date_str}"))
        new_events[date_str] = "\r\n".join([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=America/Los_Angeles:{date_str}T113000",
            f"DTEND;TZID=America/Los_Angeles:{date_str}T123000",
            f"SUMMARY:{title}",
            description(published_menu_url(menu_id)),
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ])

    # Merge
    all_events = {**existing_events, **new_events}

    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Tustin Arroyo Elementary Lunch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Arroyo Elementary Lunch Calendar",
        "X-WR-TIMEZONE:America/Los_Angeles",
        "X-PUBLISHED-TTL:PT4H",
    ])
    events = "\r\n".join([all_events[d] for d in sorted(all_events.keys())])
    return "\r\n".join([header, events, "END:VCALENDAR"])


# ─────────────────────────────────────────────
# SCHEDULE HELPERS
# ─────────────────────────────────────────────

def is_second_to_last_day(today):
    """Returns True if today is the second to last day of the month."""
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day - 1


def is_last_day_of_month(today):
    """Returns True if today is the last day of the month."""
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


def should_run_today(today):
    """
    Run on:
    - Second to last day of month at 5:30pm PT (initial search)
    - Last day of month at 5:30pm PT (second attempt)
    - 1st through 15th daily (retries until next month found)
    """
    return is_second_to_last_day(today) or is_last_day_of_month(today) or today.day <= 15


def get_next_month(month, year):
    return (1, year + 1) if month == 12 else (month + 1, year)


def get_next_month_found():
    if os.path.exists(NEXT_MONTH_FOUND_FILE):
        with open(NEXT_MONTH_FOUND_FILE, "r") as f:
            val = f.read().strip()
            if val:
                return val
    return None


def save_next_month_found(month, year):
    with open(NEXT_MONTH_FOUND_FILE, "w") as f:
        f.write(f"{month}/{year}")


def clear_next_month_found():
    if os.path.exists(NEXT_MONTH_FOUND_FILE):
        os.remove(NEXT_MONTH_FOUND_FILE)


def file_hash(filepath):
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "r", encoding="utf-8") as f:
        return hashlib.md5(f.read().encode()).hexdigest()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("Arroyo Elementary Lunch Calendar Generator")
    print("=" * 50)

    today = date.today()
    print(f"Today: {today}")

    force_run = os.environ.get("FORCE_RUN", "false").lower() == "true"

    if not force_run and not should_run_today(today):
        print(f"Today (day {today.day}) is not a scheduled run day. Skipping.")
        return

    if force_run:
        print("Manual trigger detected — forcing run regardless of date.")

    # Determine target month
    # On 27th or later look for next month, otherwise current month
    if is_second_to_last_day(today) or is_last_day_of_month(today):
        target_month, target_year = get_next_month(today.month, today.year)
    else:
        target_month, target_year = today.month, today.year

    target_label = datetime(target_year, target_month, 1).strftime("%B %Y")
    print(f"Target month: {target_label}")

    # Reset found flag on 27th
    if is_second_to_last_day(today):
        print("Starting new monthly search cycle — resetting found flag.")
        clear_next_month_found()

    # Skip if already found this month
    already_found = get_next_month_found()
    if already_found == f"{target_month}/{target_year}" and not force_run:
        print(f"Already successfully loaded {target_label} — nothing to do.")
        return

    # ── Discover the right menu for the target month ──
    print(f"\nChecking site menus via API...")
    try:
        site_menus = fetch_site_menus()
    except Exception as e:
        print(f"  Could not fetch site menus: {e}")
        site_menus = []

    target_month_str = f"{target_year}-{target_month:02d}-01"
    menu_id, published = select_menu(site_menus, target_month_str)
    print(f"  Using menu {menu_id} — published months: {published}")

    if site_menus and target_month_str not in published:
        print(f"  {target_label} not published yet — keeping existing ICS unchanged.")
        print("  Will retry at next scheduled run.")
        notify_not_found("Arroyo Elementary Lunch Calendar", target_label)
        return

    # ── Fetch menu data ────────────────────────
    print(f"\nFetching menu data for {target_label}...")
    try:
        date_overwrites = fetch_date_overwrites(menu_id, target_year, target_month)
    except Exception as e:
        print(f"  Failed to fetch menu data: {e}")
        return

    # ── Parse menu ─────────────────────────────
    daily_menu = parse_daily_menu(date_overwrites)
    print(f"Found entrees for {len(daily_menu)} school days")

    if not daily_menu:
        print("No menu items found — keeping existing ICS unchanged.")
        notify_found_failure("Arroyo Elementary Lunch Calendar", target_label, "Menu data returned no entree items.")
        return

    # ── Generate ICS ───────────────────────────
    ics_content = generate_ics(daily_menu, target_month, target_year,
                               menu_id, existing_ics_path=OUTPUT_ICS)

    os.makedirs("docs", exist_ok=True)
    old_hash = file_hash(OUTPUT_ICS)
    new_hash = hashlib.md5(ics_content.encode()).hexdigest()

    if old_hash == new_hash:
        print("No changes detected — ICS file is already up to date.")
    else:
        with open(OUTPUT_ICS, "w", encoding="utf-8") as f:
            f.write(ics_content)
        print(f"ICS file updated with {len(daily_menu)} new events.")
        # Only notify success when ICS actually changed (new month loaded)
        notify_success("Arroyo Elementary Lunch Calendar", target_label, len(daily_menu))

    save_next_month_found(target_month, target_year)
    print(f"Marked {target_month}/{target_year} as found.")

    # If we just loaded current month and it's the 27th or later,
    # immediately try to find next month too
    if today.day >= 27 and target_month == today.month and target_year == today.year:
        next_month, next_year = get_next_month(target_month, target_year)
        next_label = datetime(next_year, next_month, 1).strftime("%B %Y")
        print(f"\nIt's the 27th or later — checking if {next_label} is published yet...")
        next_month_str = f"{next_year}-{next_month:02d}-01"
        next_menu_id, next_published = select_menu(site_menus, next_month_str)
        if next_month_str in next_published:
            print(f"  {next_label} is published (menu {next_menu_id})! Fetching...")
            try:
                next_overwrites = fetch_date_overwrites(next_menu_id, next_year, next_month)
                next_daily = parse_daily_menu(next_overwrites)
                if next_daily:
                    ics_content = generate_ics(next_daily, next_month, next_year,
                                               next_menu_id, existing_ics_path=OUTPUT_ICS)
                    new_hash = hashlib.md5(ics_content.encode()).hexdigest()
                    if file_hash(OUTPUT_ICS) != new_hash:
                        with open(OUTPUT_ICS, "w", encoding="utf-8") as f:
                            f.write(ics_content)
                        print(f"  ICS updated with {next_label} events.")
                    save_next_month_found(next_month, next_year)
                    print(f"  Marked {next_month}/{next_year} as found.")
            except Exception as e:
                print(f"  Failed to fetch {next_label}: {e}")
        else:
            print(f"  {next_label} not published yet — daily retries will continue.")
            # Clear found flag so daily retries keep looking for next month
            clear_next_month_found()

    print("\nDone! ✅")


if __name__ == "__main__":
    main()
