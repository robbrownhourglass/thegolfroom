# The Golf Room — Flask Rebuild

A simple, self-contained Flask/Jinja rebuild of [thegolfroom.ie](https://www.thegolfroom.ie),
Eamonn O'Flanagan's golf coaching site in Rathevan, Co. Laois. Built as a lightweight
replacement for the original Wix site, reusing the real copy, logo, and photos.

## Pages

- `/` — Home
- `/meet-eamonn` — Meet Eamonn
- `/coaching-services` — Golf Coaching programmes
- `/free-practice-guide` — Free practice guide signup
- `/bookings` — Bookings enquiry
- `/gift-vouchers` — Gift vouchers
- `/contact` — Contact form

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5050 (not 5000 — macOS AirPlay Receiver commonly
squats on 5000).

## Bookings — connecting Eamonn's Google Calendar

`/bookings` shows real open slots (Mon–Fri, 9am–5pm, 60-minute sessions,
Europe/Dublin time) read live from Eamonn's Google Calendar, and submitting
a request writes a **tentative** hold onto that calendar for him to confirm
or decline — it never auto-books. Until it's connected, `/bookings` quietly
falls back to a plain contact form instead of breaking.

Auth is via OAuth "Sign in with Google" — Eamonn authorizes the app once with
his own Google account rather than sharing his calendar with a robot
identity. To connect it, see the full step-by-step setup guide in the
docstring at the top of `calendar_service.py`: create a Google Cloud OAuth
client, save its downloaded JSON as `client_secret.json`, set an
`ADMIN_SETUP_TOKEN` environment variable, then (as Eamonn) visit
`/connect-calendar?token=<that value>` once and click through Google's
sign-in. That setup needs Eamonn's own Google account access, so it can't be
done from here.

Business rules (hours, session length, how many days ahead to show, minimum
notice) are constants at the top of `calendar_service.py` — edit them there
if Eamonn's actual availability differs.

## Notes

- The contact/free-practice-guide/newsletter forms validate input and show a
  confirmation message, but **do not send real email yet** —
  `handle_contact_form()` in `app.py` is the place to wire up SMTP, a CRM, or
  a form service (e.g. Mailgun, SendGrid, Formspree) before using this in
  production. (Bookings are the exception — those really do write to Google
  Calendar once configured, see above.)
- The gift vouchers page links out via `mailto:` rather than an online store —
  the original site's voucher shop uses a Wix Stores widget that wasn't
  replicated here.
- Images and copy were pulled directly from the live site's public pages.
- `app.secret_key` reads from a `FLASK_SECRET_KEY` environment variable
  (falling back to a dev-only placeholder) — set that before deploying, since
  it also protects the OAuth sign-in against CSRF, not just flash messages.
- `client_secret.json` and `token.json` (created once Eamonn completes
  `/connect-calendar`) are gitignored — never commit real Google credentials.
