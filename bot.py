"""Calgary public skating schedule -> Google Calendar feed (.ics) + weekly Telegram digest.

Public Skate at City of Calgary arenas is drop-in only (no reservations, no spot counts),
so this bot just collects the upcoming schedule from the Live & Play site, writes an .ics
calendar file (subscribe to it from Google Calendar) and sends a weekly summary to Telegram.
"""
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://liveandplay.calgary.ca"
CATEGORY_GUID = "86b5d1d9-687a-40b2-aa48-f5a296b7fe13"   # Skating category
LIST_URL = (f"{BASE}/REGPROG/public/Category/ClassList?CategoryGUID={CATEGORY_GUID}"
            "&StartDate={d}&Participant=00000000-0000-0000-0000-000000000000")
PAGE_URL = f"{BASE}/REGPROG/public/category/browse/PublicSkate"
TZ = ZoneInfo("America/Edmonton")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))
ICS_PATH = os.getenv("ICS_PATH", "docs/skating.ics")
DEBUG_FILE = "debug_dump.json"
FILTER = [w.strip().lower() for w in os.getenv("FACILITY_FILTER", "").split(",") if w.strip()]
PRICES = "Adult $6.30 · Senior $4.50 · Youth $3.20 · Child $2.80 · Family $12.90"

DATE_RE = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+(\d{1,2})[-\s]([A-Za-z]{3})[A-Za-z]*[-\s,]*(\d{2,4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)\s*[-–]\s*(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
HEADINGS = {"public skate": "public", "50+ leisure skate": "other", "sticks, pucks and rings": "other",
            "sticks pucks and rings": "other", "sticks & pucks": "other"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def to_dt(d: date, h, m, ap):
    h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
    return datetime(d.year, d.month, d.day, h, int(m), tzinfo=TZ)


def parse_page(html: str):
    """Return list of dicts: start, end, venue for Public Skate sessions."""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sessions, cur_date, cur_time, cur_class = [], None, None, "public"
    for line in lines:
        low = line.lower()
        if low in HEADINGS:
            cur_class = HEADINGS[low]
            continue
        m = DATE_RE.search(line)
        if m and not TIME_RE.search(line):
            mon = MONTHS.get(m.group(3).lower())
            yr = int(m.group(4))
            yr += 2000 if yr < 100 else 0
            if mon:
                cur_date = date(yr, mon, int(m.group(2)))
            continue
        t = TIME_RE.search(line)
        if t and cur_date:
            cur_time = t.groups()
            continue
        if cur_time and cur_date and ("arena" in low or low.endswith("- ice")):
            h1, m1, a1, h2, m2, a2 = cur_time
            venue = re.sub(r"\s*-\s*Ice$", "", line, flags=re.I).strip()
            if cur_class == "public" and "50+" not in low and "sticks" not in low:
                sessions.append({"start": to_dt(cur_date, h1, m1, a1),
                                 "end": to_dt(cur_date, h2, m2, a2), "venue": venue})
            cur_time = None
    return sessions


def fetch_day(d: date) -> str:
    r = requests.get(LIST_URL.format(d=d.isoformat()), timeout=45,
                     headers={"User-Agent": "Mozilla/5.0 (skate-calendar-bot)"})
    r.raise_for_status()
    return r.text


def fetch_day_browser(d: date) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(locale="en-CA", timezone_id="America/Edmonton")
        page.goto(LIST_URL.format(d=d.isoformat()), wait_until="networkidle", timeout=60000)
        html = page.content()
        b.close()
        return html


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Edmonton
BEGIN:DAYLIGHT
TZOFFSETFROM:-0700
TZOFFSETTO:-0600
TZNAME:MDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0600
TZOFFSETTO:-0700
TZNAME:MST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def build_ics(sessions) -> str:
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//calgary-skate-bot//EN", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:Calgary Public Skating", "X-WR-TIMEZONE:America/Edmonton",
           "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H", VTIMEZONE]
    for s in sessions:
        uid = hashlib.sha1(f"{s['start'].isoformat()}{s['venue']}".encode()).hexdigest() + "@calgary-skate"
        out += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                f"DTSTART;TZID=America/Edmonton:{s['start']:%Y%m%dT%H%M%S}",
                f"DTEND;TZID=America/Edmonton:{s['end']:%Y%m%dT%H%M%S}",
                f"SUMMARY:{ics_escape('⛸️ Public Skate – ' + s['venue'])}",
                f"LOCATION:{ics_escape(s['venue'] + ', Calgary, AB')}",
                f"DESCRIPTION:{ics_escape('Drop-in only, no reservation needed. ' + PRICES + chr(10) + PAGE_URL)}",
                f"URL:{PAGE_URL}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def telegram(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram secrets missing - skipping message")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()


def digest(sessions, today: date) -> str:
    by_day = defaultdict(list)
    for s in sessions:
        if s["start"].date() < today + timedelta(days=7):
            by_day[s["start"].date()].append(s)
    lines = ["⛸️ Public skating this week (drop-in, no booking):"]
    for d in sorted(by_day):
        lines.append(f"\n{d:%a %b %d}")
        for s in sorted(by_day[d], key=lambda x: x["start"]):
            t1 = s["start"].strftime("%I:%M").lstrip("0")
            t2 = s["end"].strftime("%I:%M %p").lstrip("0")
            lines.append(f"• {t1}–{t2}  {s['venue']}")
    lines.append(f"\n{PRICES}")
    return "\n".join(lines)


def main() -> int:
    today = datetime.now(TZ).date()
    sessions, raw_samples = [], {}
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        try:
            html = fetch_day(d)
            found = parse_page(html)
            if not found and i == 0:          # maybe the page needs JS - retry once in a browser
                html = fetch_day_browser(d)
                found = parse_page(html)
        except Exception as e:
            print("failed", d, e)
            continue
        raw_samples[d.isoformat()] = BeautifulSoup(html, "html.parser").get_text("\n")[:2500]
        sessions += [s for s in found if s["start"].date() == d]

    if FILTER:
        sessions = [s for s in sessions if any(w in s["venue"].lower() for w in FILTER)]
    seen, uniq = set(), []
    for s in sorted(sessions, key=lambda x: (x["start"], x["venue"])):
        k = (s["start"], s["venue"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)

    with open(DEBUG_FILE, "w") as f:
        json.dump({"count": len(uniq), "sessions": [f"{s['start']} {s['venue']}" for s in uniq],
                   "page_text_samples": raw_samples}, f, indent=1, default=str)

    if not uniq:
        telegram("⚠️ Skating bot: no public skating sessions found - the site layout may have changed.")
        print("no sessions found")
        return 1

    os.makedirs(os.path.dirname(ICS_PATH) or ".", exist_ok=True)
    with open(ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(uniq))
    telegram(digest(uniq, today))
    print(f"wrote {len(uniq)} sessions to {ICS_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        try:
            telegram(f"⚠️ Skating bot failed: {e}")
        except Exception:
            pass
        raise
