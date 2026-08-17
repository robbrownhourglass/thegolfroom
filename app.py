"""
The Golf Room — Flask rebuild
A simple, self-contained clone of thegolfroom.ie built with Flask + Jinja templates.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""
import os

from flask import Flask, render_template, request, redirect, url_for, flash, session, abort

import calendar_service

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
# Used for flash messages AND to protect the OAuth "state" during Google
# sign-in (see /connect-calendar) — set FLASK_SECRET_KEY to a real random
# value before any real deploy, not just before going live with bookings.

# Nav links shared across every page (base.html renders these).
# Matches the live site's structure: 4 links on the bar, the rest tucked
# under a "More" dropdown (the live site overflows into "More" too, once
# nav width runs out — we just make that grouping explicit and permanent).
NAV_LINKS = [
    ("home", "Home"),
    ("meet_eamonn", "Meet Eamonn"),
    ("coaching", "Golf Coaching"),
    ("free_practice_guide", "Free Practice Guide"),
]
NAV_MORE_LINKS = [
    ("bookings", "Bookings"),
    ("gift_vouchers", "Gift Vouchers"),
    ("contact", "Contact"),
]

CONTACT_INFO = {
    "phone": "086 3843258",
    "email": "info@thegolfroom.ie",
    "address": ["The Golf Room,", "Rathevan,", "Co. Laois,", "Ireland"],
    "cro": "CRO 753871",
}


@app.context_processor
def inject_globals():
    return {
        "nav_links": NAV_LINKS,
        "nav_more_links": NAV_MORE_LINKS,
        "nav_more_endpoints": {endpoint for endpoint, _ in NAV_MORE_LINKS},
        "contact": CONTACT_INFO,
        "active": request.endpoint,
    }


def handle_contact_form(thank_you_message):
    """Shared handler for the site's various contact/enquiry forms.

    This does NOT send real email — it just validates input and flashes a
    confirmation, matching the "Thanks for submitting!" copy on the live
    site. Wire this up to an email/CRM service before using in production.
    """
    name = request.form.get("first_name", "").strip()
    email = request.form.get("email", "").strip()
    message = request.form.get("message", "").strip()

    if not name or not email:
        flash("Please fill in your name and email before submitting.", "error")
        return False

    # In a real deployment: send an email, save to a database, call a CRM API, etc.
    app.logger.info("New enquiry from %s <%s>: %s", name, email, message)
    flash(thank_you_message, "success")
    return True


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/meet-eamonn")
def meet_eamonn():
    return render_template("meet_eamonn.html")


COACHING_PROGRAMMES = [
    {
        "title": "Goal Setting",
        "img": "goal-setting.jpg",
        "body": [
            "We pride ourselves on helping you reach your goals. Sometimes you don't know what's realistic or what you are capable of achieving. But we are there to help you through the tough days and give you guidance to keep you on track.",
            "When you are clear where you would like to take your golf we can then put a bespoke programme together for you with regular reviews and support along the way.",
        ],
    },
    {
        "title": "Performance Practice",
        "img": "performance-practice.jpg",
        "body": [
            "It's not just about hitting as many balls as you can. It's about replicating the game as closely as possible whilst developing your skills to perform under pressure.",
            "We will show you how to organise your practice sessions, the different types of practice that are used, and how to integrate them into your schedule.",
        ],
    },
    {
        "title": "Short Game Skills",
        "img": "short-game.jpg",
        "body": [
            "Every shot counts on the golf course and we know how many shots you can save on and around the greens. With that in mind it's why we always make sure to cover bunker play, a variety of chip shots and flight control.",
            "We then head to the greens where we'll give you a blueprint for your putting stroke and some great ways to keep speed control and green reading fun and pressure filled.",
        ],
    },
    {
        "title": "Stats Analysis",
        "img": "stats-analysis.jpg",
        "body": [
            "“If you can't measure it you can't manage it.”",
            "We want to help you get better... fast! By helping you keep some basic stats on your game we can pinpoint your areas of strength and weakness and get straight to the areas that can have the biggest impact. Let us help you eliminate the guesswork and analyse your game in detail, showing you how to reduce your score by managing the numbers.",
        ],
    },
    {
        "title": "Distance & Speed",
        "img": "distance-speed.jpg",
        "body": [
            "We haven't worked with many golfers that wouldn't happily accept an extra 10 yards off the tee.",
            "Amongst the other areas of development we also have ways to help you quicken up your swing and add up to 40 yards off the tee. This could range from getting the correct clubs in your hands, to helping you with some basic exercises or swing changes to give you some easy gains.",
        ],
    },
    {
        "title": "Mental Skills",
        "img": "mental-skills.jpg",
        "body": [
            "Probably the most underrated part of improving your game is improving your mindset. We can help you with pre-shot routines through to dealing with pressure.",
            "Many golfers complain that they struggle to take their game from the range across to the golf course. We have lots of techniques to help you get the best out of yourself and get out of your own way.",
        ],
    },
]


@app.route("/coaching-services")
def coaching():
    return render_template("coaching.html", programmes=COACHING_PROGRAMMES)


@app.route("/free-practice-guide", methods=["GET", "POST"])
def free_practice_guide():
    if request.method == "POST":
        handle_contact_form("Thanks for subscribing! Your free practice guide is on its way.")
        return redirect(url_for("free_practice_guide"))
    return render_template("free_practice_guide.html")


@app.route("/bookings", methods=["GET", "POST"])
def bookings():
    if not calendar_service.is_configured():
        # Google Calendar isn't wired up yet (see calendar_service.py for
        # setup steps) — fall back to a plain request form instead of
        # breaking the page.
        if request.method == "POST":
            handle_contact_form("Thanks for submitting! We'll be in touch shortly.")
            return redirect(url_for("bookings"))
        return render_template("bookings_fallback.html")

    if request.method == "POST":
        slot_iso = request.form.get("slot", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        notes = request.form.get("notes", "").strip()

        if not slot_iso or not name or not email:
            flash("Please choose a time and fill in your name and email.", "error")
            return redirect(url_for("bookings"))

        success, error = calendar_service.create_booking_request(slot_iso, name, email, phone, notes)
        if success:
            flash(
                "Thanks! Your requested slot has been sent to Eamonn for confirmation — "
                "you'll hear back shortly.",
                "success",
            )
        else:
            flash(error or "Something went wrong — please try again.", "error")
        return redirect(url_for("bookings"))

    days = calendar_service.get_available_slots()
    return render_template("bookings.html", days=days)


@app.route("/connect-calendar")
def connect_calendar():
    """
    One-time admin route: kicks off the "Sign in with Google" flow that
    authorizes this app against Eamonn's Google Calendar. Gated by
    ADMIN_SETUP_TOKEN so a random visitor can't hijack the connection.
    Full setup steps are in calendar_service.py's module docstring.
    """
    expected = calendar_service.ADMIN_SETUP_TOKEN
    if not expected or request.args.get("token") != expected:
        abort(403)

    redirect_uri = url_for("oauth2callback", _external=True)
    flow = calendar_service.build_auth_flow(redirect_uri)
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # forces Google to re-issue a refresh_token even on repeat auth
    )
    session["oauth_state"] = state
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    """Google redirects here after the user allows (or denies) calendar access."""
    if request.args.get("error"):
        flash(f"Google sign-in didn't complete ({request.args['error']}). Try /connect-calendar again.", "error")
        return redirect(url_for("home"))

    state = session.get("oauth_state")
    if not state:
        abort(400)

    redirect_uri = url_for("oauth2callback", _external=True)
    flow = calendar_service.build_auth_flow(redirect_uri, state=state)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        app.logger.exception("Google OAuth token exchange failed")
        flash("Something went wrong finishing Google sign-in. Please try /connect-calendar again.", "error")
        return redirect(url_for("home"))

    calendar_service.save_credentials(flow.credentials)
    flash("Google Calendar connected! The Bookings page now shows live availability.", "success")
    return redirect(url_for("bookings"))


@app.route("/gift-vouchers")
def gift_vouchers():
    return render_template("gift_vouchers.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        handle_contact_form("Thanks for submitting!")
        return redirect(url_for("contact"))
    return render_template("contact.html")


if __name__ == "__main__":
    # Port 5000 collides with macOS AirPlay Receiver on many Macs, so default to 5050.
    app.run(debug=True, port=5050)
