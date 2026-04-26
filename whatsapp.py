import os
import json
import secrets
import hashlib
import base64
import requests
from datetime import date, datetime, timedelta
from flask import Flask, request, session, render_template_string
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
AUTH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jekyll — Connect Calendar</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    background: #0b0d10;
    font-family: 'DM Sans', sans-serif;
    color: #e8e4de;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem 1.5rem;
    position: relative;
    overflow: hidden;
  }
  .bg-grid {
    position: fixed; inset: 0;
    background-image: linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
    background-size: 48px 48px; pointer-events: none;
  }
  .bg-glow { position: fixed; width: 500px; height: 500px; border-radius: 50%; background: radial-gradient(circle, rgba(52,168,83,0.07) 0%, transparent 70%); top: -120px; right: -80px; pointer-events: none; }
  .bg-glow-2 { position: fixed; width: 400px; height: 400px; border-radius: 50%; background: radial-gradient(circle, rgba(66,133,244,0.05) 0%, transparent 70%); bottom: -100px; left: -80px; pointer-events: none; }
  .card { position: relative; width: 100%; max-width: 420px; background: #12151a; border: 0.5px solid rgba(255,255,255,0.1); border-radius: 20px; padding: 3rem 2.5rem; animation: fadeUp 0.6s cubic-bezier(0.22,1,0.36,1) both; }
  @keyframes fadeUp { from { opacity: 0; transform: translateY(24px); } to { opacity: 1; transform: translateY(0); } }
  .logo-row { display: flex; align-items: center; gap: 10px; margin-bottom: 2.5rem; }
  .logo-icon { width: 32px; height: 32px; background: #e8e4de; border-radius: 8px; display: flex; align-items: center; justify-content: center; }
  .logo-name { font-family: 'Playfair Display', serif; font-size: 20px; font-weight: 600; color: #e8e4de; }
  .heading { font-family: 'Playfair Display', serif; font-size: 28px; font-weight: 400; line-height: 1.3; color: #e8e4de; margin-bottom: 0.75rem; }
  .heading em { font-style: italic; color: #a8c5a0; }
  .subtext { font-size: 14px; color: rgba(232,228,222,0.5); line-height: 1.6; margin-bottom: 2rem; }
  .divider { height: 0.5px; background: rgba(255,255,255,0.07); margin-bottom: 2rem; }
  .phone-pill { display: inline-flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.05); border: 0.5px solid rgba(255,255,255,0.1); border-radius: 100px; padding: 6px 14px; font-size: 13px; color: rgba(232,228,222,0.6); margin-bottom: 2rem; }
  .phone-dot { width: 6px; height: 6px; border-radius: 50%; background: #4caf70; animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .connect-btn { display: flex; align-items: center; justify-content: center; gap: 12px; width: 100%; padding: 14px 20px; background: #e8e4de; color: #0b0d10; border: none; border-radius: 12px; font-family: 'DM Sans', sans-serif; font-size: 15px; font-weight: 500; cursor: pointer; text-decoration: none; transition: background 0.2s, transform 0.15s; }
  .connect-btn:hover { background: #ffffff; transform: translateY(-1px); }
  .connect-btn:active { transform: scale(0.99); }
  .steps { margin-top: 2rem; display: flex; flex-direction: column; gap: 12px; }
  .step { display: flex; align-items: flex-start; gap: 12px; }
  .step-num { width: 22px; height: 22px; border-radius: 50%; border: 0.5px solid rgba(255,255,255,0.15); display: flex; align-items: center; justify-content: center; font-size: 11px; color: rgba(232,228,222,0.4); flex-shrink: 0; margin-top: 1px; }
  .step-text { font-size: 13px; color: rgba(232,228,222,0.45); line-height: 1.5; }
  .footer { margin-top: 2.5rem; font-size: 11px; color: rgba(232,228,222,0.2); text-align: center; line-height: 1.6; }
</style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="bg-glow"></div>
  <div class="bg-glow-2"></div>
  <div class="card">
    <div class="logo-row">
      <div class="logo-icon">
        <svg viewBox="0 0 18 18" width="18" height="18" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="2" y="4" width="14" height="11" rx="2" stroke="#0b0d10" stroke-width="1.5"/>
          <path d="M6 2v4M12 2v4" stroke="#0b0d10" stroke-width="1.5" stroke-linecap="round"/>
          <path d="M2 8h14" stroke="#0b0d10" stroke-width="1.5"/>
          <circle cx="6.5" cy="12" r="1" fill="#0b0d10"/>
          <circle cx="9.5" cy="12" r="1" fill="#0b0d10"/>
        </svg>
      </div>
      <span class="logo-name">Jekyll</span>
    </div>
    <h1 class="heading">Your calendar,<br><em>by text message.</em></h1>
    <p class="subtext">Connect Google Calendar once, then just text naturally — Jekyll handles the rest.</p>
    <div class="divider"></div>
    <div class="phone-pill">
      <div class="phone-dot"></div>
      Connecting for {{ phone }}
    </div>
    <a href="{{ auth_url }}" class="connect-btn">
      <svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
        <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z" fill="#4285F4"/>
        <path d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.258c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z" fill="#34A853"/>
        <path d="M3.964 10.707c-.18-.54-.282-1.117-.282-1.707s.102-1.167.282-1.707V4.961H.957C.347 6.175 0 7.55 0 9s.348 2.825.957 4.039l3.007-2.332z" fill="#FBBC05"/>
        <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.961L3.964 7.293C4.672 5.163 6.656 3.58 9 3.58z" fill="#EA4335"/>
      </svg>
      Connect Google Calendar
    </a>
    <div class="steps">
      <div class="step"><div class="step-num">1</div><div class="step-text">Sign in with your Google account and grant calendar access</div></div>
      <div class="step"><div class="step-num">2</div><div class="step-text">Close this tab and return to WhatsApp</div></div>
      <div class="step"><div class="step-num">3</div><div class="step-text">Text Jekyll anything — "dentist Friday at 3pm" — and you're live</div></div>
    </div>
    <p class="footer">Jekyll only reads and writes calendar events.<br>Your data is never stored or shared.</p>
  </div>
</body>
</html>"""

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
    return render_template_string(AUTH_PAGE, phone=phone, auth_url=auth_url)

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