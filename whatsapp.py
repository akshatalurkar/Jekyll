import os
import json
import secrets
import hashlib
import base64
import requests
from datetime import date, datetime, timedelta
from flask import Flask, request, session, render_template
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from requests_oauthlib import OAuth2Session
from zoneinfo import ZoneInfo

load_dotenv()

database_url = os.getenv("DATABASE_URL", "sqlite:///users.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    oauth_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)
    last_event = db.Column(db.JSON, nullable=True)


class ProcessedMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")


# ── Gemini helpers ──────────────────────────────────────────

def classify_intent(message):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    prompt = f"""You are Jekyll, a WhatsApp calendar assistant. Classify the user's message into exactly one category.

Current date: {today}
Current time: {current_time} PT

Return ONLY the category word. No punctuation, no explanation.

CATEGORIES:
GREETING - hello, hi, hey, what's up, how are you, who are you, what can you do
CREATE_TODAY - adding an event that happens today
CREATE_TOMORROW - adding an event that happens tomorrow
CREATE_RELATIVE - adding an event in X hours or X minutes from now
CREATE_SPECIFIC - adding an event on a specific future date or day of the week
DELETE - canceling, removing, or deleting an existing event
LIST_TODAY - asking what's on the calendar today
LIST_TOMORROW - asking what's on the calendar tomorrow
LIST_WEEK - asking what's coming up this week or in the next few days
CONFIRM - confirming a pending action: "yes", "yep", "yeah", "correct", "sure", "ok", "do it"
UNKNOWN - anything else

EXAMPLES:
"hi" → GREETING
"hey what's up" → GREETING
"what can you do" → GREETING
"dentist today at 3pm" → CREATE_TODAY
"lunch at noon" → CREATE_TODAY
"coffee tmrw morning" → CREATE_TOMORROW
"dentist tomorrow 2pm" → CREATE_TOMORROW
"meeting in 2 hours" → CREATE_RELATIVE
"call in 30 mins" → CREATE_RELATIVE
"dentist friday 3pm" → CREATE_SPECIFIC
"coffee with jake next monday" → CREATE_SPECIFIC
"cancel my dentist" → DELETE
"remove the 3pm" → DELETE
"what do i have today" → LIST_TODAY
"anything today?" → LIST_TODAY
"what's tomorrow look like" → LIST_TOMORROW
"anything tomorrow?" → LIST_TOMORROW
"what do i have this week" → LIST_WEEK
"what's coming up" → LIST_WEEK
"yes" → CONFIRM
"yeah do it" → CONFIRM
"thanks" → UNKNOWN
"lol" → UNKNOWN

Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip().upper()


def parse_event(message, intent, last_event=None):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%H:%M")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    in_2h = (now + timedelta(hours=2)).strftime("%H:%M")
    in_30m = (now + timedelta(minutes=30)).strftime("%H:%M")
    last_event_context = f"\nLast event discussed: {json.dumps(last_event)}" if last_event else ""

    if intent == "CREATE_TODAY":
        date_hint = f"The event is TODAY ({today}). Use this as the date."
    elif intent == "CREATE_TOMORROW":
        date_hint = f"The event is TOMORROW ({tomorrow}). Use this as the date."
    elif intent == "CREATE_RELATIVE":
        date_hint = f"The event is relative to NOW ({current_time} today {today}). Calculate the exact time."
    else:
        date_hint = "Resolve the date from the message using the current date as reference."

    prompt = f"""You are Jekyll, a WhatsApp calendar assistant. Parse this message into a calendar event.

Current date: {today} ({today_display})
Current time: {current_time} (Pacific Time)
{date_hint}{last_event_context}

Be robust to typos, abbreviations, missing punctuation, all lowercase, all caps.
If a field is ambiguous, make your best inference. Only return null if there is truly no information.
If the message references a previous event, use that context to fill missing fields.

Return ONLY a valid JSON object. No explanations, comments, markdown, or text outside the JSON.

OUTPUT SCHEMA:
{{
  "title": string,
  "date": "YYYY-MM-DD" | null,
  "time": "HH:MM" | null,
  "duration_minutes": integer | null,
  "location": string | null,
  "confidence": "high" | "medium" | "low"
}}

TITLE:
- Clean, properly capitalized
- Preserve context ("Coffee with Maya", not "Coffee")
- Strip filler: "let's", "gonna", "need to", "gotta", "have to", "want to", "remember to"

TIME:
- "morning" → 09:00, "afternoon" → 14:00, "evening" → 18:00, "night" → 20:00, "noon" → 12:00, "eod" → 17:00
- "in 2 hours" → {in_2h}, "in 30 mins" → {in_30m} (round to nearest 5 min)
- Ambiguous number ("at 8", "at 7") → resolve by type:
  - gym, run, yoga, breakfast, standup → AM
  - dinner, drinks, bar, party, movie → PM
  - meeting, call, sync, lunch → ≤7 assume PM, 8-11 assume AM, 12 = noon

DURATION:
- Explicit value always wins
- default → 60

LOCATION:
- Only if explicitly mentioned
- Never infer
- null if not mentioned

CONFIDENCE:
- "high" → title, date, time all clear
- "medium" → one field inferred
- "low" → multiple fields inferred or message is very vague

Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)


def parse_delete(message):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%A, %B %d, %Y")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = f"""You are Jekyll, a WhatsApp calendar assistant. Extract what the user wants to delete.

Current date: {today} ({today_display})

Return ONLY a valid JSON object. No explanations, comments, or markdown.

OUTPUT SCHEMA:
{{
  "title": string,
  "date": "YYYY-MM-DD" | null
}}

TITLE: Short, searchable, lowercase keyword(s) only.

EXAMPLES:
"cancel my dentist friday" → {{"title": "dentist", "date": null}}
"remove the team sync tomorrow" → {{"title": "team sync", "date": "{tomorrow}"}}
"delete my 3pm today" → {{"title": "3pm", "date": "{today}"}}
"get rid of coffee with jake" → {{"title": "coffee with jake", "date": null}}

Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)


# ── Google Calendar helpers ─────────────────────────────────

def get_calendar_service(user):
    creds = Credentials(
        token=user.oauth_token,
        refresh_token=user.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    )
    return build("calendar", "v3", credentials=creds)

def check_conflict(service, new_event):
    start = datetime.strptime(
        f"{new_event['date']} {new_event['time']}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=ZoneInfo("America/Los_Angeles"))

    end = start + timedelta(minutes=new_event.get("duration_minutes") or 60)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=(start - timedelta(hours=1)).isoformat(),
        timeMax=(end + timedelta(hours=1)).isoformat(),
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    events = events_result.get("items", [])
    return events

def handle_create(user, text, phone, intent):
    event_data = parse_event(text, intent, last_event=user.last_event)

    if not event_data.get("title"):
        send_whatsapp(phone, "What's the event?")
        return
    
    if not event_data.get("date") or not event_data.get("time"):
        send_whatsapp(phone, "Got it — what date and time should I set this for?")
        return
    
    # --- Check if event is in the past or happening now ---
    event_dt = datetime.strptime(
        f"{event_data['date']} {event_data['time']}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=ZoneInfo("America/Los_Angeles"))

    now = datetime.now(ZoneInfo("America/Los_Angeles"))

    # CASE 1: Event is in the past
    if event_dt < now:
        user.last_event = {
            "needs_time_confirm": True,
            "new_event": event_data
        }
        db.session.commit()

        send_whatsapp(
            phone,
            f"This is scheduled in the past.\n\n"
            f"{event_data['title']} on {event_data['date']} at {event_data['time']}\n\n"
            f"Reply *Yes* to add it anyway, or send a correction."
        )
        return

    # CASE 2: Event is happening right now (within 5 minutes)
    if now <= event_dt <= now + timedelta(minutes=5):
        user.last_event = {
            "needs_time_confirm": True,
            "new_event": event_data
        }
        db.session.commit()

        send_whatsapp(
            phone,
            f"This looks like it's happening right now.\n\n"
            f"{event_data['title']} at {event_data['time']}\n\n"
            f"Reply *Yes* to add it anyway, or send a correction."
        )
        return
    
    service = get_calendar_service(user)
    conflicts = check_conflict(service, event_data)

    if conflicts:
        existing = conflicts[0]

        existing_start = existing["start"].get("dateTime", existing["start"].get("date"))

        dt = datetime.fromisoformat(existing_start.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Los_Angeles"))

        existing_time = dt.strftime("%a %b %d at %I:%M %p")

        user.last_event = {
            "new_event": event_data,
            "conflict_event": {
                "title": existing["summary"],
                "time": existing_time
            },
            "needs_double_confirm": True
        }
        db.session.commit()

        send_whatsapp(
            phone,
            f"You already have {existing['summary']} scheduled for {existing_time}.\n\n"
            f"Do you still want to add {event_data['title']} on {event_data['date']} at {event_data['time']}?\n\n"
            f"Reply *Yes* to confirm, or send a correction."
        )
        return        

    user.last_event = event_data
    db.session.commit()
    
    duration = event_data.get("duration_minutes") or 60
    duration_line = f"{duration} min (default)" if not event_data.get("duration_minutes") else f"{duration} min"
    location_line = f"\n{event_data['location']}" if event_data.get("location") else ""

    send_whatsapp(
        phone,
        f"Here's what I'll add:\n\n"
        f"*{event_data['title']}*\n"
        f"{event_data['date']} at {event_data['time']}\n"
        f"{duration_line}"
        f"{location_line}\n\n"
        f"Reply *Yes* to confirm, or send a correction."
    )


def handle_confirm(user, phone):
    if not user.last_event:
        send_whatsapp(phone, "Nothing pending — what would you like to add?")
        return
    event_data = user.last_event

    # Handle past-time confirmation case
    if isinstance(event_data, dict) and event_data.get("needs_time_confirm"):
        event_data = event_data["new_event"]

# Handle double booking case
    if isinstance(event_data, dict) and event_data.get("needs_double_confirm"):
        event_data = event_data["new_event"]

    if not event_data.get("date") or not event_data.get("time"):
        send_whatsapp(phone, "What date and time? 🗓️")
        return
    service = get_calendar_service(user)
    start = datetime.strptime(f"{event_data['date']} {event_data['time']}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=event_data.get("duration_minutes") or 60)
    event = {
        "summary": event_data["title"],
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
    }
    if event_data.get("location"):
        event["location"] = event_data["location"]
    service.events().insert(calendarId="primary", body=event).execute()
    user.last_event = None
    db.session.commit()
    location_str = f" at {event_data['location']}" if event_data.get("location") else ""
    send_whatsapp(phone, f"Added - {event_data['title']}{location_str} on {event_data['date']} at {event_data['time']}")


def handle_delete(user, text, phone):
    delete_data = parse_delete(text)
    title = delete_data.get("title", "").lower()
    service = get_calendar_service(user)
    now = datetime.utcnow().isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=10,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    match = next((e for e in events if title in e.get("summary", "").lower()), None)
    if not match:
        send_whatsapp(phone, f"Couldn't find '{delete_data['title']}' coming up.")
        return
    service.events().delete(calendarId="primary", eventId=match["id"]).execute()
    send_whatsapp(phone, f"Removed {match['summary']} ")


def handle_list(user, phone, intent):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    service = get_calendar_service(user)

    if intent == "LIST_TODAY":
        time_min = now.replace(hour=0, minute=0, second=0).isoformat()
        time_max = now.replace(hour=23, minute=59, second=59).isoformat()
        label = "today"
    elif intent == "LIST_TOMORROW":
        tomorrow = now + timedelta(days=1)
        time_min = tomorrow.replace(hour=0, minute=0, second=0).isoformat()
        time_max = tomorrow.replace(hour=23, minute=59, second=59).isoformat()
        label = "tomorrow"
    else:
        time_min = now.isoformat()
        time_max = (now + timedelta(days=7)).isoformat()
        label = "this week"

    events_result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        maxResults=5,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])

    if not events:
        send_whatsapp(phone, f"Nothing on the calendar {label}.")
        return

    lines = [f"Here's {label} on your calendar:"]
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Los_Angeles"))
        lines.append(f"• {e['summary']} — {dt.strftime('%a %b %d at %I:%M %p')}")
    send_whatsapp(phone, "\n".join(lines))


# ── WhatsApp ────────────────────────────────────────────────

def send_whatsapp(to, text):
    response = requests.post(
        f"https://graph.facebook.com/v18.0/{os.getenv('WHATSAPP_PHONE_NUMBER_ID')}/messages",
        headers={
            "Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}",
            "Content-Type": "application/json"
        },
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }
    )
    return response.json()


# ── Routes ──────────────────────────────────────────────────


@app.route("/auth/<phone>")
def auth(phone):
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    oauth = OAuth2Session(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        redirect_uri=f"{BASE_URL}/oauth/callback",
        scope=["https://www.googleapis.com/auth/calendar.events"]
    )
    auth_url, state = oauth.authorization_url(
        "https://accounts.google.com/o/oauth2/auth",
        access_type="offline",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256"
    )
    session["state"] = state
    session["phone"] = phone
    session["code_verifier"] = code_verifier
    return render_template("auth.html", phone=phone, auth_url=auth_url)


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
            return challenge, 200
        return "Forbidden", 403

    body = request.json

    try:
        entry = body["entry"][0]
        change = entry["changes"][0]
        message = change["value"]["messages"][0]
        phone = message["from"]
        if not phone.startswith("+"):
            phone = "+" + phone
        text = message["text"]["body"]
        message_id = message["id"]
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    if ProcessedMessage.query.filter_by(message_id=message_id).first():
        return "OK", 200
    db.session.add(ProcessedMessage(message_id=message_id))
    db.session.commit()

    user = User.query.filter_by(phone=phone).first()

    if not user or not user.oauth_token:
        send_whatsapp(
            phone,
            f"Welcome to Jekyll — your text-to-calendar assistant!\n\n"
            f"To get started, connect your Google Calendar here: {BASE_URL}/auth/{phone}\n\n"
            f"Once you're set up, just text me what you'd like to add to your calendar.\n\n"
            f"Examples: \"Dentist Friday at 3pm\", \"Food at noon for 45 mins at Chipotle\"\n\n"
            f"For detailed instructions on how to use this, click here: https://your-link-here"
        )
        return "OK", 200

    try:
        intent = classify_intent(text)

        if intent == "GREETING":
            send_whatsapp(phone, "Hey! What would you like to schedule?")

        elif intent in ("CREATE_TODAY", "CREATE_TOMORROW", "CREATE_RELATIVE", "CREATE_SPECIFIC"):
            handle_create(user, text, phone, intent)

        elif intent == "CONFIRM":
            handle_confirm(user, phone)

        elif intent == "DELETE":
            handle_delete(user, text, phone)

        elif intent in ("LIST_TODAY", "LIST_TOMORROW", "LIST_WEEK"):
            handle_list(user, phone, intent)

        else:
            send_whatsapp(phone, "Not sure what you mean. Try:\n• 'dentist Friday 3pm'\n• 'what's on today?'\n• 'cancel my dentist'")

    except Exception as e:
        print(f"Error: {e}")
        send_whatsapp(phone, "Something went wrong — try again in a moment.")

    return "OK", 200


@app.route("/oauth/callback")
def oauth_callback():
    phone = session.get("phone")
    code_verifier = session.get("code_verifier")
    oauth = OAuth2Session(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        redirect_uri=f"{BASE_URL}/oauth/callback",
        state=session.get("state")
    )
    token = oauth.fetch_token(
        "https://oauth2.googleapis.com/token",
        authorization_response=request.url,
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        code_verifier=code_verifier
    )
    user = User.query.filter_by(phone=phone).first()
    if not user:
        user = User(phone=phone)
        db.session.add(user)
    user.oauth_token = token["access_token"]
    user.refresh_token = token.get("refresh_token")
    db.session.commit()
    return render_template("success.html")


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8001)))