"""
Google Calendar integration for The Golf Room's booking page.

How this works
---------------
Eamonn's calendar is treated as the single source of truth for availability.
A Google service account (a "robot" Google identity, not a personal login)
is granted access to his calendar, and this module:

  1. Reads busy/free time on his calendar (`get_available_slots`) to work out
     which 60-minute coaching slots are open, Mon-Fri 9am-5pm Europe/Dublin.
  2. Writes a TENTATIVE hold onto his calendar when someone requests a slot
     (`create_booking_request`) — it does NOT auto-confirm. Eamonn reviews
     pending requests (shown clearly as "PENDING REQUEST" events, and via
     Google's own notification email to him as an invited attendee) and
     confirms or declines them directly in Google Calendar.

One-time setup required (cannot be done by this code — needs your Google
account access):

  1. Go to https://console.cloud.google.com/ and create a project (or reuse
     one).
  2. Enable the "Google Calendar API" for that project (APIs & Services ->
     Enable APIs and Services -> search "Google Calendar API" -> Enable).
  3. Create a service account (APIs & Services -> Credentials -> Create
     Credentials -> Service account). Give it any name, e.g. "golf-room-
     bookings".
  4. Open the service account -> Keys -> Add Key -> Create new key -> JSON.
     This downloads a .json file — save it as `service_account.json` in
     this project's root folder (it's already in .gitignore, so it will
     never get committed/pushed).
  5. Open the downloaded JSON file and copy the "client_email" value
     (looks like `golf-room-bookings@your-project.iam.gserviceaccount.com`).
  6. In Google Calendar (as Eamonn), go to the calendar you want bookings
     read from/written to -> Settings and sharing -> "Share with specific
     people" -> add that service account email -> permission
     "Make changes to events".
  7. Set two environment variables before running the app:
       GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json   (or an absolute path)
       GOOGLE_CALENDAR_ID=eamonns-address@gmail.com       (the calendar's ID —
         usually just the Google account email the calendar belongs to)

Until those are set, `is_configured()` returns False and the booking page
falls back to a plain contact form instead of showing a slot picker.
"""
import os
import time
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo
from google.oauth2 import service_account
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

SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None  # lazily-built, cached Calendar API client
_slots_cache = {"expires": 0, "value": None}
CACHE_SECONDS = 120  # avoid hammering the API on every page view


def is_configured():
    """True once the service account file + calendar ID are both in place."""
    return bool(CALENDAR_ID) and os.path.isfile(SERVICE_ACCOUNT_FILE)


def _get_service():
    global _service
    if _service is None:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
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
    earliest_bookable = datetime.now(BUSINESS_TZ) + timedelta(hours=MIN_NOTICE_HOURS)

    days = []
    for offset in range(DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in BUSINESS_DAYS:
            continue
        day_slots = []
        for slot_start in _business_slot_starts(day):
            slot_end = slot_start + timedelta(minutes=SESSION_MINUTES)
            if slot_start < earliest_bookable:
                continue
            if _overlaps(slot_start, slot_end, busy_periods):
                continue
            day_slots.append({
                "start": slot_start,
                "end": slot_end,
                "iso": slot_start.isoformat(),
                "label": slot_start.strftime("%-I:%M %p"),
            })
        days.append({
            "date": day,
            "label": day.strftime("%A %-d %B"),
            "slots": day_slots,
        })

    _slots_cache["value"] = days
    _slots_cache["expires"] = now + CACHE_SECONDS
    return days


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
