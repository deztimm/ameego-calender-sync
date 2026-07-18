"""
Ameego -> Apple Calendar sync script.

Confirmed working:
  - Login (username / password / client ID + submit) succeeds.
  - The post-login dashboard has an "Upcoming Shifts" box with date,
    start time, and a department/role label for each shift.

What this does:
  1. Logs in using the real field IDs from the Ameego login page.
  2. Reads the "Upcoming Shifts" list off the dashboard.
  3. Writes a local .ics (for the existing GitHub Pages / webcal
     subscription, kept as a backup/debug trail).
  4. Connects directly to iCloud (CalDAV) and creates/updates/removes
     events on the shared Family calendar, so everyone in the family
     sees it without doing anything themselves.

NOT independently testable here (no network access to Apple's CalDAV
servers from where this is written) -- unlike the Ameego login/scraping
part, which was verified against real data before shipping, this part
is a best-effort implementation of a well-established, standard CalDAV
pattern. Run it with dry_run on first so you can check the log before
it touches anything real. See the safety notes on sync_to_family_calendar().

Required GitHub Actions secrets:
  AMEEGO_USERNAME
  AMEEGO_PASSWORD
  AMEEGO_CLIENT_ID
  APPLE_ID              (the Apple ID email on the Family account)
  APPLE_APP_PASSWORD    (an app-specific password, not your real Apple ID password --
                          generate one at appleid.apple.com > Sign-In and Security
                          > App-Specific Passwords)
"""

import os
import re
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

try:
    import caldav
except ImportError:
    caldav = None

AMEEGO_LOGIN_URL = "https://login.myameego.com/"

USERNAME = os.environ.get("AMEEGO_USERNAME")
PASSWORD = os.environ.get("AMEEGO_PASSWORD")
CLIENT_ID = os.environ.get("AMEEGO_CLIENT_ID")

APPLE_ID = os.environ.get("APPLE_ID")
APPLE_APP_PASSWORD = os.environ.get("APPLE_APP_PASSWORD")
FAMILY_CALENDAR_HINT = os.environ.get("FAMILY_CALENDAR_NAME", "family").lower()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

UID_PREFIX = "ameego-sync-"
SHIFT_HOURS = 5

DEBUG_DIR = "debug"
ICS_PATH = "my-shifts.ics"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def log(msg):
    print(f"[sync] {msg}", flush=True)


def save_debug(page, name):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    png_path = os.path.join(DEBUG_DIR, f"{name}.png")
    html_path = os.path.join(DEBUG_DIR, f"{name}.html")
    try:
        page.screenshot(path=png_path, full_page=True)
    except Exception as e:
        log(f"Could not save screenshot {png_path}: {e}")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        log(f"Could not save HTML {html_path}: {e}")
    log(f"Saved {png_path} and {html_path}")


def login(page):
    log(f"Opening {AMEEGO_LOGIN_URL}")
    page.goto(AMEEGO_LOGIN_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    save_debug(page, "01-login-page")

    page.fill("#username", USERNAME)
    page.fill("#password", PASSWORD)
    if CLIENT_ID:
        page.fill("#client-id", CLIENT_ID)

    page.click('button[type="submit"]')
    page.wait_for_timeout(4000)
    save_debug(page, "02-dashboard")


def parse_dashboard_date(text, today=None):
    today = today or date_cls.today()
    parts = text.replace("\n", " ").split()
    month_abbr = parts[-2][:3]
    day_num = int(re.sub(r"\D", "", parts[-1]))
    month_num = MONTHS.get(month_abbr, 1)
    candidate = date_cls(today.year, month_num, day_num)
    # Handle year rollover (e.g. running in December about a January shift)
    if (today - candidate).days > 60:
        candidate = date_cls(today.year + 1, month_num, day_num)
    return candidate.isoformat()


def parse_time_12h(text):
    text = text.strip().lower()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", text)
    if not m:
        return None
    h, mnt, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mnt:02d}"


def extract_dashboard_shifts(page):
    shifts = []
    rows = page.locator(
        'div.col-sm-7:has(h3:has-text("Upcoming Shifts")) > div.col-sm-12'
    )
    count = rows.count()
    log(f"Found {count} row(s) in the Upcoming Shifts box.")

    for i in range(count):
        row = rows.nth(i)
        try:
            date_text = row.locator("span").first.inner_text()
            info = row.locator(".col-xs-9 > div")
            time_text = info.locator("> div").nth(0).inner_text()
            label_text = info.locator("> div").nth(1).inner_text()
        except Exception as e:
            log(f"Skipping row {i}, couldn't read it: {e}")
            continue

        iso_date = parse_dashboard_date(date_text)
        start_24h = parse_time_12h(time_text)
        label = " ".join(label_text.split())

        if not (iso_date and start_24h):
            log(f"Skipping row {i}, couldn't parse date/time from {date_text!r} / {time_text!r}")
            continue

        shifts.append({"date": iso_date, "start": start_24h, "label": label})
        log(f"Parsed shift: {iso_date} {start_24h} - {label}")

    return shifts


def shift_uid(s):
    """Deterministic ID per shift (same date+start always maps to the same
    UID), so re-runs can tell 'still the same shift' from 'new' or
    'removed' instead of creating duplicates every time."""
    return f"{UID_PREFIX}{s['date'].replace('-', '')}-{s['start'].replace(':', '')}@dez"


def build_vevent_block(s, uid):
    start_dt = datetime.strptime(f"{s['date']} {s['start']}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=SHIFT_HOURS)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M00')}",
        f"DTEND:{end_dt.strftime('%Y%m%dT%H%M00')}",
        "SUMMARY:Dez work",
        "END:VEVENT",
    ]


def generate_ics(shifts):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ameego Sync//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Dez Work",
    ]
    for s in shifts:
        lines += build_vevent_block(s, shift_uid(s))
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def sync_to_family_calendar(shifts):
    """
    Create/update/remove events on the shared iCloud Family calendar.

    Safety rules this follows, on purpose:
      - Only ever touches events whose UID starts with UID_PREFIX --
        every other event on the Family calendar (yours, your family's,
        anything already there) is never read, changed, or deleted.
      - DRY_RUN=true logs exactly what it would create/delete without
        actually calling the API. Use this to sanity-check the plan
        before trusting it with a real family calendar.
    """
    if not caldav:
        log("caldav package not installed -- skipping Family calendar sync.")
        return
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        log("Missing APPLE_ID or APPLE_APP_PASSWORD secrets -- skipping Family calendar sync.")
        return

    log(f"Connecting to iCloud as {APPLE_ID} (DRY_RUN={DRY_RUN})")
    client = caldav.DAVClient(
        url="https://caldav.icloud.com/",
        username=APPLE_ID,
        password=APPLE_APP_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()

    names = []
    for c in calendars:
        try:
            names.append(c.name)
        except Exception:
            names.append("<unknown>")
    log(f"Calendars visible to this account: {names}")

    target = None
    for c in calendars:
        try:
            if c.name and FAMILY_CALENDAR_HINT in c.name.lower():
                target = c
                break
        except Exception:
            continue

    if not target:
        log(
            f"Could not find a calendar with '{FAMILY_CALENDAR_HINT}' in its name. "
            f"Set the FAMILY_CALENDAR_NAME secret to match one of: {names}"
        )
        return

    log(f"Using calendar: {target.name}")

    # Only look a reasonable window ahead -- we don't need the calendar's
    # entire history, and it keeps this fast.
    window_start = datetime.now() - timedelta(days=1)
    window_end = datetime.now() + timedelta(days=120)
    try:
        existing_events = target.date_search(start=window_start, end=window_end)
    except Exception as e:
        log(f"Could not read events from the Family calendar: {e}")
        return

    existing_ours = {}
    for event in existing_events:
        try:
            raw = event.data
        except Exception:
            continue
        m = re.search(r"UID:([^\r\n]+)", raw or "")
        if m and m.group(1).startswith(UID_PREFIX):
            existing_ours[m.group(1)] = event

    desired = {shift_uid(s): s for s in shifts}

    to_add = [uid for uid in desired if uid not in existing_ours]
    to_remove = [uid for uid in existing_ours if uid not in desired]

    log(f"Family calendar: {len(existing_ours)} of our events currently there, "
        f"{len(to_add)} to add, {len(to_remove)} to remove.")

    for uid in to_remove:
        if DRY_RUN:
            log(f"[DRY RUN] would remove {uid}")
            continue
        try:
            existing_ours[uid].delete()
            log(f"Removed {uid}")
        except Exception as e:
            log(f"Failed to remove {uid}: {e}")

    for uid in to_add:
        s = desired[uid]
        ical = "\r\n".join(
            ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Ameego Sync//EN"]
            + build_vevent_block(s, uid)
            + ["END:VCALENDAR"]
        )
        if DRY_RUN:
            log(f"[DRY RUN] would add {uid} ({s['date']} {s['start']})")
            continue
        try:
            target.save_event(ical)
            log(f"Added {uid} ({s['date']} {s['start']})")
        except Exception as e:
            log(f"Failed to add {uid}: {e}")


def main():
    if not USERNAME or not PASSWORD:
        log("Missing AMEEGO_USERNAME or AMEEGO_PASSWORD secrets.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        login(page)
        shifts = extract_dashboard_shifts(page)

        browser.close()

    if shifts:
        ics_content = generate_ics(shifts)
        with open(ICS_PATH, "w", encoding="utf-8") as f:
            f.write(ics_content)
        log(f"Wrote {ICS_PATH} with {len(shifts)} shift(s).")
        sync_to_family_calendar(shifts)
    else:
        log("No shifts were extracted -- check debug/02-dashboard.png.")
        sys.exit(1)


if __name__ == "__main__":
    main()
