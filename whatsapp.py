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


def handle_create(user, text, phone, intent):
    event_data = parse_event(text, intent, last_event=user.last_event)

    if not event_data.get("title"):
        send_whatsapp(phone, "What's the event?")
        return
    
    if not event_data.get("date") or not event_data.get("time"):
        send_whatsapp(phone, "What date and time?")
        return
    
    if user.last_event:
        send_whatsapp(
            phone,
            f"You still have an event pending confirmation: *{user.last_event['title']}* on {user.last_event['date']}.\n\n"
            f"Reply *Yes* to confirm, or *Skip* to discard and add the new one."
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
    send_whatsapp(phone, f"Done ✅ {event_data['title']}{location_str} — {event_data['date']} at {event_data['time']}")


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
    send_whatsapp(phone, f"Removed {match['summary']} 🗑️")


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

    lines = [f"Here's {label} 📅"]
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

AUTH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jekyll — Connect Calendar</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { min-height: 100vh; background: #0b0d10; font-family: 'DM Sans', sans-serif; color: #e8e4de; display: flex; align-items: center; justify-content: center; padding: 2rem 1.5rem; position: relative; overflow-y: auto; }
  .bg-grid { position: fixed; inset: 0; background-image: linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px); background-size: 48px 48px; pointer-events: none; }
  .bg-glow { position: fixed; width: 560px; height: 560px; border-radius: 50%; background: radial-gradient(circle, rgba(37,211,102,0.13) 0%, rgba(37,211,102,0.04) 45%, transparent 70%); top: -150px; right: -100px; pointer-events: none; }
  .bg-glow-2 { position: fixed; width: 480px; height: 480px; border-radius: 50%; background: radial-gradient(circle, rgba(18,140,65,0.11) 0%, rgba(18,140,65,0.04) 45%, transparent 70%); bottom: -120px; left: -100px; pointer-events: none; }
  .card { position: relative; width: 100%; max-width: 420px; background: #12151a; border: 0.5px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 3rem 2.5rem; animation: fadeUp 0.6s cubic-bezier(0.22,1,0.36,1) both; }
  @keyframes fadeUp { from { opacity: 0; transform: translateY(24px); } to { opacity: 1; transform: translateY(0); } }
  .logo-row { display: flex; align-items: center; gap: 10px; margin-bottom: 2.5rem; }
  .logo-icon { width: 32px; height: 32px; background: #25D366; border-radius: 8px; display: flex; align-items: center; justify-content: center; }
  .logo-name { font-family: 'Syne', sans-serif; font-size: 22px; font-weight: 700; letter-spacing: -0.5px; color: #e8e4de; }
  .heading { font-family: 'Syne', sans-serif; font-size: 30px; font-weight: 600; line-height: 1.25; color: #e8e4de; margin-bottom: 0.75rem; letter-spacing: -0.5px; }
  .heading em { font-style: normal; color: #25D366; }
  .subtext { font-size: 14px; color: rgba(232,228,222,0.45); line-height: 1.6; margin-bottom: 2rem; }
  .divider { height: 0.5px; background: rgba(255,255,255,0.07); margin-bottom: 2rem; }
  .phone-pill { display: inline-flex; align-items: center; gap: 8px; background: rgba(37,211,102,0.07); border: 0.5px solid rgba(37,211,102,0.2); border-radius: 100px; padding: 6px 14px; font-size: 13px; color: rgba(232,228,222,0.55); margin-bottom: 2rem; }
  .phone-dot { width: 6px; height: 6px; border-radius: 50%; background: #25D366; animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .connect-btn { display: flex; align-items: center; justify-content: center; gap: 12px; width: 100%; padding: 14px 20px; background: #e8e4de; color: #0b0d10; border: none; border-radius: 12px; font-family: 'DM Sans', sans-serif; font-size: 15px; font-weight: 500; cursor: pointer; text-decoration: none; transition: background 0.2s, transform 0.15s; }
  .connect-btn:hover { background: #ffffff; transform: translateY(-1px); }
  .steps { margin-top: 2rem; display: flex; flex-direction: column; gap: 12px; }
  .step { display: flex; align-items: flex-start; gap: 12px; }
  .step-num { width: 22px; height: 22px; border-radius: 50%; border: 0.5px solid rgba(37,211,102,0.2); display: flex; align-items: center; justify-content: center; font-size: 11px; color: rgba(37,211,102,0.45); flex-shrink: 0; margin-top: 1px; font-family: 'Syne', sans-serif; }
  .step-text { font-size: 13px; color: rgba(232,228,222,0.4); line-height: 1.5; }
  .footer { margin-top: 2.5rem; font-size: 11px; color: rgba(232,228,222,0.18); text-align: center; line-height: 1.6; }
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

SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jekyll — Connected!</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { min-height: 100vh; background: #0b0d10; font-family: 'DM Sans', sans-serif; color: #e8e4de; display: flex; align-items: center; justify-content: center; padding: 2rem 1.5rem; position: relative; overflow-y: auto; }
  .bg-grid { position: fixed; inset: 0; background-image: linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px); background-size: 48px 48px; pointer-events: none; }
  .bg-glow { position: fixed; width: 560px; height: 560px; border-radius: 50%; background: radial-gradient(circle, rgba(37,211,102,0.18) 0%, rgba(37,211,102,0.06) 45%, transparent 70%); top: -150px; right: -100px; pointer-events: none; }
  .card { position: relative; width: 100%; max-width: 420px; background: #12151a; border: 0.5px solid rgba(255,255,255,0.08); border-radius: 20px; padding: 3rem 2.5rem; animation: fadeUp 0.6s cubic-bezier(0.22,1,0.36,1) both; text-align: center; }
  @keyframes fadeUp { from { opacity: 0; transform: translateY(24px); } to { opacity: 1; transform: translateY(0); } }
  .check-ring { width: 72px; height: 72px; border-radius: 50%; background: rgba(37,211,102,0.1); border: 0.5px solid rgba(37,211,102,0.3); display: flex; align-items: center; justify-content: center; margin: 0 auto 2rem; animation: popIn 0.5s cubic-bezier(0.34,1.56,0.64,1) 0.3s both; }
  @keyframes popIn { from { opacity: 0; transform: scale(0.6); } to { opacity: 1; transform: scale(1); } }
  .logo-row { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 2rem; }
  .logo-icon { width: 28px; height: 28px; background: #25D366; border-radius: 7px; display: flex; align-items: center; justify-content: center; }
  .logo-name { font-family: 'Syne', sans-serif; font-size: 20px; font-weight: 700; letter-spacing: -0.5px; color: #e8e4de; }
  .heading { font-family: 'Syne', sans-serif; font-size: 26px; font-weight: 600; line-height: 1.25; color: #e8e4de; margin-bottom: 0.6rem; letter-spacing: -0.5px; }
  .heading em { font-style: normal; color: #25D366; }
  .subtext { font-size: 14px; color: rgba(232,228,222,0.45); line-height: 1.6; margin-bottom: 2rem; }
  .divider { height: 0.5px; background: rgba(255,255,255,0.07); margin-bottom: 2rem; }
  .examples-label { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: rgba(232,228,222,0.25); margin-bottom: 1rem; font-family: 'Syne', sans-serif; }
  .bubbles { display: flex; flex-direction: column; gap: 8px; text-align: left; }
  .bubble { background: rgba(37,211,102,0.06); border: 0.5px solid rgba(37,211,102,0.15); border-radius: 12px 12px 12px 3px; padding: 10px 14px; font-size: 13px; color: rgba(232,228,222,0.7); }
  .close-note { margin-top: 2rem; font-size: 12px; color: rgba(232,228,222,0.2); line-height: 1.6; }
</style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="bg-glow"></div>
  <div class="card">
    <div class="logo-row">
      <div class="logo-icon">
        <svg viewBox="0 0 18 18" width="16" height="16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="2" y="4" width="14" height="11" rx="2" stroke="#0b0d10" stroke-width="1.5"/>
          <path d="M6 2v4M12 2v4" stroke="#0b0d10" stroke-width="1.5" stroke-linecap="round"/>
          <path d="M2 8h14" stroke="#0b0d10" stroke-width="1.5"/>
          <circle cx="6.5" cy="12" r="1" fill="#0b0d10"/>
          <circle cx="9.5" cy="12" r="1" fill="#0b0d10"/>
        </svg>
      </div>
      <span class="logo-name">Jekyll</span>
    </div>
    <div class="check-ring">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 16.5l5.5 5.5 10-11" stroke="#25D366" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <h1 class="heading">You're <em>all set.</em></h1>
    <p class="subtext">Google Calendar is connected. Head back to WhatsApp and start texting Jekyll.</p>
    <div class="divider"></div>
    <p class="examples-label">Try saying</p>
    <div class="bubbles">
      <div class="bubble">dentist appointment Friday at 3pm</div>
      <div class="bubble">what do I have today?</div>
      <div class="bubble">cancel my 3pm tomorrow</div>
    </div>
    <p class="close-note">You can close this tab and return to WhatsApp.</p>
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
        message_id = message["id"]
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    # Deduplicate
    if ProcessedMessage.query.filter_by(message_id=message_id).first():
        return "OK", 200
    db.session.add(ProcessedMessage(message_id=message_id))
    db.session.commit()

    user = User.query.filter_by(phone=phone).first()

    if not user or not user.oauth_token:
        send_whatsapp(phone, f"👋 Welcome to Jekyll — your text-to-calendar assistant!\n\nTo get started, connect your Google Calendar:\n{BASE_URL}/auth/{phone}\n\nOnce connected, just text me anything you want to add. Example: 'dentist Friday at 3pm'")
        return "OK", 200

    try:
        intent = classify_intent(text)

        if intent == "GREETING":
            send_whatsapp(phone, "Hey! 👋 What would you like to schedule?")

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
    return render_template_string(SUCCESS_PAGE)


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8001)))