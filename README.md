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

Then open http://127.0.0.1:5000

## Notes

- The contact/booking/newsletter forms validate input and show a confirmation
  message, but **do not send real email yet** — `handle_contact_form()` in
  `app.py` is the place to wire up SMTP, a CRM, or a form service (e.g.
  Mailgun, SendGrid, Formspree) before using this in production.
- The gift vouchers page links out via `mailto:` rather than an online store —
  the original site's voucher shop uses a Wix Stores widget that wasn't
  replicated here.
- Images and copy were pulled directly from the live site's public pages.
- `app.secret_key` in `app.py` is a placeholder — replace it with a real
  secret (e.g. from an environment variable) before deploying.
