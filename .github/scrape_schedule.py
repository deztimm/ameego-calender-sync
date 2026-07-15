"""
Ameego -> Apple Calendar sync script (PHASE 1: login recon).

What this does right now:
  1. Opens the Ameego login page in a real (headless) browser, since the
     site is a JavaScript app and won't respond to plain HTTP requests.
  2. Tries a handful of common patterns to find and fill the
     username / password / client ID fields, since the real markup
     hasn't been inspected yet.
  3. Saves a screenshot + full HTML of what it sees at each step into
     debug/ , win or lose.
  4. If login looks like it worked, it stops there and reports it, so
     we can see the schedule page's real structure before writing the
     part that reads shifts off of it.

What this does NOT do yet:
  Extract real shift data or generate a calendar file. That's phase 2,
  once we can see debug/03-after-login.png (and, ideally, the schedule
  page after that) and know what we're actually parsing. The
  generate_ics() helper below is already written and ready for that --
  it just isn't called with real data yet.

Required GitHub Actions secrets (Settings -> Secrets and variables -> Actions):
  AMEEGO_USERNAME
  AMEEGO_PASSWORD
  AMEEGO_CLIENT_ID   (optional -- only if your login screen asks for one)
"""

import os
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

AMEEGO_LOGIN_URL = "https://login.myameego.com/"

USERNAME = os.environ.get("AMEEGO_USERNAME")
PASSWORD = os.environ.get("AMEEGO_PASSWORD")
CLIENT_ID = os.environ.get("AMEEGO_CLIENT_ID")

DEBUG_DIR = "debug"


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


def find_and_fill(page, selectors, value, label):
    """Try selectors in order; fill + report the first visible match."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1500):
                loc.fill(value)
                log(f"Filled {label} using selector: {sel}")
                return True
        except Exception:
            continue
    log(f"Could not find a field for {label} using any known selector.")
    return False


def attempt_login(page):
    log(f"Opening {AMEEGO_LOGIN_URL}")
    page.goto(AMEEGO_LOGIN_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)  # let the JS app finish rendering
    save_debug(page, "01-login-page")

    if CLIENT_ID:
        find_and_fill(
            page,
            [
                'input[name*="client" i]',
                'input[id*="client" i]',
                'input[placeholder*="client" i]',
            ],
            CLIENT_ID,
            "Client ID",
        )

    filled_user = find_and_fill(
        page,
        [
            'input[name*="user" i]',
            'input[id*="user" i]',
            'input[placeholder*="user" i]',
            'input[type="email"]',
            'input[type="text"]',
        ],
        USERNAME,
        "Username",
    )

    filled_pass = find_and_fill(
        page,
        ['input[type="password"]'],
        PASSWORD,
        "Password",
    )

    if not (filled_user and filled_pass):
        save_debug(page, "02-fields-not-found")
        log(
            "Stopping: couldn't find both username and password fields. "
            "Check debug/02-fields-not-found.png and .html."
        )
        return False

    try:
        page.locator(
            'button[type="submit"], '
            'button:has-text("Sign in"), '
            'button:has-text("Log in"), '
            'button:has-text("Login")'
        ).first.click(timeout=3000)
        log("Clicked a likely submit button.")
    except Exception:
        log("No obvious submit button found, pressing Enter instead.")
        page.keyboard.press("Enter")

    page.wait_for_timeout(4000)
    save_debug(page, "03-after-login")
    return True


def generate_ics(shifts):
    """
    Turn a list of shift dicts into an .ics file (text/calendar).
    Each shift: {"date": "2026-07-14", "start": "16:00", "end": "22:00",
                 "label": "Work Shift"}
    Not wired up to real data yet -- kept ready for phase 2.
    """

    def pad(n):
        return str(n).zfill(2)

    def escape(text):
        return (
            str(text)
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ameego Sync//EN",
        "CALSCALE:GREGORIAN",
    ]

    for i, s in enumerate(shifts):
        date_compact = s["date"].replace("-", "")
        start_compact = s["start"].replace(":", "") + "00"
        end_compact = s["end"].replace(":", "") + "00"
        overnight = s["end"] <= s["start"]
        end_date_compact = date_compact
        if overnight:
            y, m, d = (int(s["date"][0:4]), int(s["date"][5:7]), int(s["date"][8:10]))
            from datetime import date, timedelta

            nd = date(y, m, d) + timedelta(days=1)
            end_date_compact = f"{nd.year}{pad(nd.month)}{pad(nd.day)}"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{now.timestamp()}-{i}@ameego-sync",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{date_compact}T{start_compact}",
            f"DTEND:{end_date_compact}T{end_compact}",
            f"SUMMARY:{escape(s.get('label') or 'Work Shift')}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def main():
    if not USERNAME or not PASSWORD:
        log("Missing AMEEGO_USERNAME or AMEEGO_PASSWORD secrets. Set them in "
            "Settings -> Secrets and variables -> Actions, then re-run.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        ok = attempt_login(page)
        browser.close()

    if ok:
        log(
            "Login attempted -- check debug/03-after-login.png to see where "
            "we landed. Share that (and 01-login-page.png if login failed) "
            "so the real shift-scraping step can be written next."
        )
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
