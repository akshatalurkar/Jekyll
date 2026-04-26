import os
import json
import secrets
import hashlib
import base64
import requests
from datetime import date, datetime, timedelta
from flask import Flask, request, session
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from google import genai
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from requests_oauthlib import OAuth2Session

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

with app.app_context():
    db.create_all()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")

# ── Gemini helpers ──────────────────────────────────────────

def classify_intent(message):
    prompt = f"""Classify this message into one of these categories:
CREATE - user wants to add a calendar event
DELETE - user wants to cancel or remove an event
LIST - user wants to see upcoming events
RECURRING - user wants to add a repeating event
UNKNOWN - none of the above

Return ONLY the category word, nothing else.
Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip().upper()

def parse_event(message):
    now = datetime.now()
    today = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%H:%M")
    prompt = f"""Today is {today} and the current time is {current_time}.
Extract calendar event details from this message and return ONLY a JSON object with these fields:
title, date (YYYY-MM-DD), time (HH:MM, 24hr), duration_minutes.
Set any missing fields to null.
Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)

def parse_delete(message):
    now = datetime.now()
    today = now.strftime("%A, %B %d, %Y")
    prompt = f"""Today is {today}.
Extract the event the user wants to delete from this message and return ONLY a JSON object with:
title (the event name to search for), date (YYYY-MM-DD, or null if not specified).
Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)

def parse_recurring(message):
    now = datetime.now()
    today = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%H:%M")
    prompt = f"""Today is {today} and the current time is {current_time}.
Extract recurring event details from this message and return ONLY a JSON object with:
title, time (HH:MM, 24hr), duration_minutes, 
recurrence (one of: DAILY, WEEKLY, MONTHLY),
day_of_week (e.g. MO, TU, WE, TH, FR, SA, SU — only for WEEKLY, else null).
Set any missing fields to null.
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

def handle_create(user, text, phone):
    event_data = parse_event(text)
    if not event_data.get("date") or not event_data.get("time"):
        send_whatsapp(phone, "I couldn't figure out the date or time — can you be more specific?")
        return
    service = get_calendar_service(user)
    start = datetime.strptime(f"{event_data['date']} {event_data['time']}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=event_data.get("duration_minutes") or 60)
    event = {
        "summary": event_data["title"],
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
    }
    service.events().insert(calendarId="primary", body=event).execute()
    send_whatsapp(phone, f"✅ Added: {event_data['title']} on {event_data['date']} at {event_data['time']}")

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
        send_whatsapp(phone, f"I couldn't find an upcoming event matching '{delete_data['title']}'.")
        return
    service.events().delete(calendarId="primary", eventId=match["id"]).execute()
    send_whatsapp(phone, f"🗑️ Deleted: {match['summary']}")

def handle_list(user, phone):
    service = get_calendar_service(user)
    now = datetime.utcnow().isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary",
        timeMin=now,
        maxResults=5,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    events = events_result.get("items", [])
    if not events:
        send_whatsapp(phone, "You have no upcoming events.")
        return
    lines = ["📅 Your next events:"]
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date"))
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        lines.append(f"• {e['summary']} — {dt.strftime('%a %b %d at %I:%M %p')}")
    send_whatsapp(phone, "\n".join(lines))

def handle_recurring(user, text, phone):
    event_data = parse_recurring(text)
    if not event_data.get("time") or not event_data.get("recurrence"):
        send_whatsapp(phone, "I couldn't figure out the time or frequency — can you be more specific?")
        return
    service = get_calendar_service(user)
    today = date.today()
    start = datetime.strptime(f"{today} {event_data['time']}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=event_data.get("duration_minutes") or 60)
    recurrence = event_data["recurrence"]
    if recurrence == "WEEKLY" and event_data.get("day_of_week"):
        rrule = f"RRULE:FREQ=WEEKLY;BYDAY={event_data['day_of_week']}"
    elif recurrence == "DAILY":
        rrule = "RRULE:FREQ=DAILY"
    elif recurrence == "MONTHLY":
        rrule = "RRULE:FREQ=MONTHLY"
    else:
        rrule = "RRULE:FREQ=WEEKLY"
    event = {
        "summary": event_data["title"],
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
        "recurrence": [rrule],
    }
    service.events().insert(calendarId="primary", body=event).execute()
    send_whatsapp(phone, f"🔁 Recurring event added: {event_data['title']} — {recurrence.lower()}")

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
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    user = User.query.filter_by(phone=phone).first()

    if not user or not user.oauth_token:
        send_whatsapp(phone, f"👋 Welcome to Jekyll — your text-to-calendar assistant!\n\nTo get started, connect your Google Calendar by clicking this link:\n{BASE_URL}/auth/{phone}\n\nOnce connected, just text me anything you want to add to your calendar. Example: 'dentist Friday at 3pm'")
        return "OK", 200

    try:
        intent = classify_intent(text)
        if intent == "CREATE":
            handle_create(user, text, phone)
        elif intent == "DELETE":
            handle_delete(user, text, phone)
        elif intent == "LIST":
            handle_list(user, phone)
        elif intent == "RECURRING":
            handle_recurring(user, text, phone)
        else:
            send_whatsapp(phone, "I didn't understand that. Try something like:\n• 'dentist Friday at 3pm'\n• 'every Monday gym at 7am'\n• 'cancel my dentist appointment'\n• 'what do I have this week?'")
    except Exception as e:
        print(f"Error: {e}")
        send_whatsapp(phone, "Something went wrong — please try again in a moment.")

    return "OK", 200

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
    return f'<a href="{auth_url}">Connect your Google Calendar</a>'

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
    return "Calendar connected! You can close this tab."

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8001)))