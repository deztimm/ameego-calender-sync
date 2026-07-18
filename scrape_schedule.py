"""
Ameego -> Apple Calendar sync script.

Confirmed working:
  - Login (username / password / client ID + submit) succeeds.
  - The post-login dashboard has an "Upcoming Shifts" box with date,
    start time, and a department/role label for each shift.

What this does:
  1. Logs in using the real field IDs from the Ameego login page.
  2. Reads the "Upcoming Shifts" list off the dashboard and builds a
     calendar (.ics) file from it -- one event per shift, titled
     "Dez work", starting at the real shift start time.
  3. The dashboard doesn't show when a shift ends, and we're not
     chasing that down -- every event gets a fixed 5-hour block
     instead.

Required GitHub Actions secrets:
  AMEEGO_USERNAME
  AMEEGO_PASSWORD
  AMEEGO_CLIENT_ID
"""

import os
import re
import sys
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

AMEEGO_LOGIN_URL = "https://login.myameego.com/"

USERNAME = os.environ.get("AMEEGO_USERNAME")
PASSWORD = os.environ.get("AMEEGO_PASSWORD")
CLIENT_ID = os.environ.get("AMEEGO_CLIENT_ID")

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


def generate_ics(shifts, placeholder_hours=5):
    def pad(n):
        return str(n).zfill(2)

    def escape(text):
        return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ameego Sync//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Dez Work",
    ]

    for i, s in enumerate(shifts):
        start_dt = datetime.strptime(f"{s['date']} {s['start']}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=placeholder_hours)

        dtstart = start_dt.strftime("%Y%m%dT%H%M00")
        dtend = end_dt.strftime("%Y%m%dT%H%M00")
        summary = escape("Dez work")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{now.timestamp()}-{i}@ameego-sync",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{summary}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


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
    else:
        log("No shifts were extracted -- check debug/02-dashboard.png.")
        sys.exit(1)


if __name__ == "__main__":
    main()
