"""
Google Calendar integration for The Golf Room's booking page.

How this works
---------------
Eamonn's calendar is treated as the single source of truth for availability.
This uses OAuth "Sign in with Google" — Eamonn authorizes the app once with
his own Google account (rather than sharing his calendar with a robot
service-account identity), and this module:

  1. Reads busy/free time on his calendar (`get_available_slots`) to work out
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
made-up sample availability (see `get_demo_slots` below) so the booking
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
DAYS_AHEAD = 14  # how far out the slot picker looks
MIN_NOTICE_HOURS = 24  # can't book a slot less than this far in advance

CLIENT_SECRETS_FILE = os.environ.get("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
ADMIN_SETUP_TOKEN = os.environ.get("ADMIN_SETUP_TOKEN")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None  # lazily-built, cached Calendar API client
_slots_cache = {"expires": 0, "value": None}
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


def _candidate_business_days(days_ahead=DAYS_AHEAD):
    """Business days (skipping weekends) from today out to days_ahead, each
    paired with its bookable candidate slot start times (respecting min notice)."""
    today = datetime.now(BUSINESS_TZ).date()
    earliest_bookable = datetime.now(BUSINESS_TZ) + timedelta(hours=MIN_NOTICE_HOURS)
    out = []
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in BUSINESS_DAYS:
            continue
        candidates = [s for s in _business_slot_starts(day) if s >= earliest_bookable]
        out.append((day, candidates))
    return out


def _slot_dict(slot_start):
    return {
        "start": slot_start,
        "end": slot_start + timedelta(minutes=SESSION_MINUTES),
        "iso": slot_start.isoformat(),
        "label": slot_start.strftime("%-I:%M %p"),
    }


def get_available_slots(use_cache=True):
    """
    Returns a list of {date, label, slots: [{start, end, iso, label}, ...]}
    for each business day between now and DAYS_AHEAD days out, with slots
    that are already busy (or too soon / in the past) filtered out.
    """
    now = time.time()
    if use_cache and _slots_cache["value"] is not None and _slots_cache["expires"] > now:
        return _slots_cache["value"]

    today = datetime.now(BUSINESS_TZ).date()
    range_start = datetime.now(BUSINESS_TZ)
    range_end = datetime.combine(
        today + timedelta(days=DAYS_AHEAD), datetime.min.time(), tzinfo=BUSINESS_TZ
    )
    busy_periods = _fetch_busy_periods(range_start, range_end)

    days = []
    for day, candidates in _candidate_business_days():
        day_slots = []
        for slot_start in candidates:
            slot_end = slot_start + timedelta(minutes=SESSION_MINUTES)
            if _overlaps(slot_start, slot_end, busy_periods):
                continue
            day_slots.append(_slot_dict(slot_start))
        days.append({"date": day, "label": day.strftime("%A %-d %B"), "slots": day_slots})

    _slots_cache["value"] = days
    _slots_cache["expires"] = now + CACHE_SECONDS
    return days


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


def get_demo_slots():
    days = []
    for day, candidates in _candidate_business_days():
        rng = random.Random(day.toordinal())  # stable per calendar day, not per request
        pre_booked = set(rng.sample(candidates, k=min(DEMO_BOOKED_PER_DAY, len(candidates))))
        day_slots = [
            _slot_dict(s) for s in candidates
            if s not in pre_booked and s.isoformat() not in _demo_requested_slots
        ]
        days.append({"date": day, "label": day.strftime("%A %-d %B"), "slots": day_slots})
    return days


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
        _slots_cache["expires"] = 0  # invalidate cache so this slot disappears immediately
        return True, None
    except HttpError as exc:
        return False, f"Google Calendar error — please try again or contact us directly. ({exc.status_code})"
