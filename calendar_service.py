"""
Google Calendar integration for The Golf Room's booking page.

How this works
---------------
Eamonn's calendar is treated as the single source of truth for availability.
This uses OAuth "Sign in with Google" — Eamonn authorizes the app once with
his own Google account (rather than sharing his calendar with a robot
service-account identity), and this module:

  1. Reads busy/free time on his calendar (`get_week_slots`) to work out
     which 60-minute coaching slots are open, Mon-Fri 9am-5pm Europe/Dublin.
  2. Writes a TENTATIVE hold onto his calendar when someone requests a slot
     (`create_booking_request`) — it does NOT auto-confirm. Eamonn reviews
     pending requests (shown clearly as "PENDING REQUEST" events, and via
     Google's own notification email to him as an invited attendee) and
     confirms or declines them directly in Google Calendar.

One-time setup required (cannot be done by this code — needs Eamonn's own
Google account access):

  1. Go to https://console.cloud.google.com/ and create a project (or reuse
     one).
  2. Enable the "Google Calendar API" for that project (APIs & Services ->
     Enable APIs and Services -> search "Google Calendar API" -> Enable).
  3. Configure the OAuth consent screen (APIs & Services -> OAuth consent
     screen): User type "External" is fine, keep it in "Testing" mode (no
     Google review needed for that), add scope
     `https://www.googleapis.com/auth/calendar`, and add Eamonn's Google
     account email under "Test users".
  4. Create credentials (APIs & Services -> Credentials -> Create Credentials
     -> OAuth client ID). Application type: "Web application". Under
     "Authorized redirect URIs" add:
       http://127.0.0.1:5050/oauth2callback        (for local testing)
       https://<your-real-domain>/oauth2callback    (once deployed)
  5. Download the client's JSON and save it as `client_secret.json` in this
     project's root folder (it's already in .gitignore, so it never gets
     committed/pushed).
  6. Pick a long random secret string and set it as an environment variable
     — this gates the one-time "connect calendar" link so a stranger can't
     hijack the connection:
       ADMIN_SETUP_TOKEN=<some long random string>
  7. Start the app, then visit (as Eamonn, logged into his own Google
     account in the browser):
       http://127.0.0.1:5050/connect-calendar?token=<the ADMIN_SETUP_TOKEN above>
     Click through Google's sign-in and the "Google hasn't verified this
     app" warning (expected — Continue -> Continue; this is normal while
     the consent screen is in Testing mode) and Allow calendar access.
     You'll land back on a "Calendar connected" confirmation, and a
     `token.json` file is created (also gitignored) holding the refresh
     token this module uses from then on.
  8. Optional: set GOOGLE_CALENDAR_ID if bookings should read/write a
     specific calendar rather than the Google account's main one — it
     defaults to "primary" (Eamonn's own main calendar).

Until step 7 is complete, `is_configured()` returns False and the booking
page runs in DEMO MODE instead: it shows the real slot-picker UI against
made-up sample availability (see `get_demo_week_slots` below) so the booking
workflow can be built and tested end-to-end before Google Calendar is
actually connected. The page clearly labels itself as demo/sample data —
see the `demo` flag app.py passes to bookings.html.
"""
import os
import random
import time
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---- Business rules (edit these to match Eamonn's real availability) ----
BUSINESS_TZ = ZoneInfo("Europe/Dublin")
BUSINESS_DAYS = {0, 1, 2, 3, 4}  # Monday=0 ... Sunday=6 (Mon-Fri)
BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 17  # last session starts an hour before this
SESSION_MINUTES = 60
WEEKS_AHEAD = 6  # how many weeks forward the calendar view can page
MIN_NOTICE_HOURS = 24  # can't book a slot less than this far in advance

CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
ADMIN_SETUP_TOKEN = os.environ.get("ADMIN_SETUP_TOKEN")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None  # lazily-built, cached Calendar API client
_week_cache = {}  # {week_offset: {"expires": ..., "value": ...}}
CACHE_SECONDS = 120  # avoid hammering the API on every page view


def is_configured():
    """True once Eamonn has completed the one-time /connect-calendar authorization."""
    if not os.path.isfile(TOKEN_FILE):
        return False
    try:
        return Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) is not None
    except (ValueError, OSError):
        return False


def _load_credentials():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def save_credentials(creds):
    """Persist freshly-authorized credentials and drop the cached API client."""
    global _service
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    _service = None


def build_auth_flow(redirect_uri, state=None):
    """Used by the /connect-calendar and /oauth2callback routes in app.py."""
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=redirect_uri, state=state
    )


def _get_service():
    global _service
    if _service is None:
        if not os.path.isfile(TOKEN_FILE):
            raise RuntimeError(
                "Google Calendar isn't connected yet — an admin needs to visit "
                "/connect-calendar to authorize it (see calendar_service.py)."
            )
        credentials = _load_credentials()
        _service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return _service


def _business_slot_starts(day):
    """All candidate 60-minute slot start times (tz-aware) for one calendar day."""
    starts = []
    hour = BUSINESS_START_HOUR
    while hour + (SESSION_MINUTES / 60) <= BUSINESS_END_HOUR:
        starts.append(datetime(day.year, day.month, day.day, hour, 0, tzinfo=BUSINESS_TZ))
        hour += 1
    return starts


def _fetch_busy_periods(start, end):
    """Busy intervals on the configured calendar between two tz-aware datetimes."""
    service = _get_service()
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": "UTC",
        "items": [{"id": CALENDAR_ID}],
    }
    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"].get(CALENDAR_ID, {}).get("busy", [])
    periods = []
    for b in busy:
        periods.append((
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
        ))
    return periods


def _overlaps(slot_start, slot_end, busy_periods):
    for busy_start, busy_end in busy_periods:
        if slot_start < busy_end and slot_end > busy_start:
            return True
    return False


def _week_monday(week_offset):
    """The Monday (tz-naive date) of the week `week_offset` weeks from this one."""
    today = datetime.now(BUSINESS_TZ).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday + timedelta(weeks=week_offset)


def clamp_week(week_offset):
    """Weeks are pageable from this week (0) out to WEEKS_AHEAD-1."""
    return max(0, min(week_offset, WEEKS_AHEAD - 1))


def _build_week_grid(week_offset, is_busy):
    """
    Shared grid-builder for both real and demo availability: `is_busy(slot_start)`
    decides whether each candidate hour is taken. Returns a Google-Calendar-style
    week view — Mon-Fri columns, one row per bookable hour — ready for the template:
      {week_offset, week_start_label, week_end_label,
       days: [{date, dow, day_num}, ...],                     # 5 entries
       hours: [{label, cells: [{status, iso}, ...]}, ...]}     # 8ish rows x 5 cells
    `status` is one of "open" (clickable), "booked", or "past" (too soon/elapsed).
    """
    monday = _week_monday(week_offset)
    days = [monday + timedelta(days=i) for i in range(5)]
    earliest_bookable = datetime.now(BUSINESS_TZ) + timedelta(hours=MIN_NOTICE_HOURS)

    hours = []
    for slot_start_today in _business_slot_starts(days[0]):
        hour = slot_start_today.hour
        cells = []
        for day in days:
            slot_start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=BUSINESS_TZ)
            if slot_start < earliest_bookable:
                status = "past"
            elif is_busy(slot_start):
                status = "booked"
            else:
                status = "open"
            cells.append({"status": status, "iso": slot_start.isoformat()})
        hours.append({"label": slot_start_today.strftime("%-I %p"), "cells": cells})

    return {
        "week_offset": week_offset,
        "week_start_label": monday.strftime("%-d %b"),
        "week_end_label": days[-1].strftime("%-d %b %Y"),
        "days": [{"date": d, "dow": d.strftime("%a"), "day_num": d.strftime("%-d")} for d in days],
        "hours": hours,
    }


def get_week_slots(week_offset=0, use_cache=True):
    """Real Google-Calendar-backed week grid (see _build_week_grid)."""
    week_offset = clamp_week(week_offset)
    now = time.time()
    cached = _week_cache.get(week_offset)
    if use_cache and cached and cached["expires"] > now:
        return cached["value"]

    monday = _week_monday(week_offset)
    range_start = datetime.combine(monday, datetime.min.time(), tzinfo=BUSINESS_TZ)
    range_end = datetime.combine(monday + timedelta(days=5), datetime.min.time(), tzinfo=BUSINESS_TZ)
    busy_periods = _fetch_busy_periods(range_start, range_end)

    def is_busy(slot_start):
        return _overlaps(slot_start, slot_start + timedelta(minutes=SESSION_MINUTES), busy_periods)

    grid = _build_week_grid(week_offset, is_busy)
    _week_cache[week_offset] = {"value": grid, "expires": now + CACHE_SECONDS}
    return grid


# ---------------------------------------------------------------------------
# Demo mode — used automatically while is_configured() is False, so the
# booking workflow can be built/tested end-to-end before Eamonn's real
# Google Calendar is connected. A couple of slots per day are randomly (but
# stably — same result on every reload) marked "already booked", and
# anything requested through the demo form is remembered in memory for the
# rest of this process's lifetime (resets on server restart). No Google API
# calls happen in this mode at all.
# ---------------------------------------------------------------------------
DEMO_BOOKED_PER_DAY = 2
_demo_requested_slots = set()  # iso strings the tester has "requested" this session


def get_demo_week_slots(week_offset=0):
    week_offset = clamp_week(week_offset)

    def is_busy(slot_start):
        day = slot_start.date()
        rng = random.Random(day.toordinal())  # stable per calendar day, not per request
        candidates = _business_slot_starts(day)
        pre_booked = set(rng.sample(candidates, k=min(DEMO_BOOKED_PER_DAY, len(candidates))))
        return slot_start in pre_booked or slot_start.isoformat() in _demo_requested_slots

    return _build_week_grid(week_offset, is_busy)


def create_demo_booking_request(slot_start_iso, name, email, phone, notes):
    """Demo-mode stand-in for create_booking_request — never touches Google."""
    try:
        slot_start = datetime.fromisoformat(slot_start_iso)
    except ValueError:
        return False, "That slot looks invalid — please pick a time again."

    earliest_bookable = datetime.now(BUSINESS_TZ) + timedelta(hours=MIN_NOTICE_HOURS)
    if slot_start < earliest_bookable:
        return False, "That slot is too soon to book online — please call or email us instead."

    if slot_start_iso in _demo_requested_slots:
        return False, "Sorry, that slot was just taken. Please pick another time."

    _demo_requested_slots.add(slot_start_iso)
    return True, None


def create_booking_request(slot_start_iso, name, email, phone, notes):
    """
    Re-checks the slot is still free, then writes a TENTATIVE hold to the
    calendar (does not auto-confirm — Eamonn approves in Google Calendar).
    Returns (success: bool, error_message: str | None).
    """
    try:
        slot_start = datetime.fromisoformat(slot_start_iso)
    except ValueError:
        return False, "That slot looks invalid — please pick a time again."

    slot_end = slot_start + timedelta(minutes=SESSION_MINUTES)
    earliest_bookable = datetime.now(BUSINESS_TZ) + timedelta(hours=MIN_NOTICE_HOURS)
    if slot_start < earliest_bookable:
        return False, "That slot is too soon to book online — please call or email us instead."

    try:
        busy_periods = _fetch_busy_periods(slot_start, slot_end)
        if _overlaps(slot_start, slot_end, busy_periods):
            return False, "Sorry, that slot was just taken. Please pick another time."

        service = _get_service()
        event = {
            "summary": f"PENDING REQUEST: {name}",
            "description": (
                f"Coaching session request submitted via the website.\n\n"
                f"Name: {name}\nEmail: {email}\nPhone: {phone or '—'}\n\n"
                f"Notes: {notes or '—'}\n\n"
                f"This event is TENTATIVE. Confirm or decline it in Google "
                f"Calendar once you've reviewed it."
            ),
            "start": {"dateTime": slot_start.isoformat()},
            "end": {"dateTime": slot_end.isoformat()},
            "status": "tentative",
            "attendees": [{"email": email, "responseStatus": "tentative"}],
            "reminders": {"useDefault": True},
        }
        service.events().insert(
            calendarId=CALENDAR_ID, body=event, sendUpdates="all"
        ).execute()
        _week_cache.clear()  # invalidate cache so this slot disappears immediately
        return True, None
    except HttpError as exc:
        return False, f"Google Calendar error — please try again or contact us directly. ({exc.status_code})"
