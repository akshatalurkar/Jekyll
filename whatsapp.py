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

with app.app_context():
    db.create_all()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")

# ── Gemini helpers ──────────────────────────────────────────

def classify_intent(message):
    prompt = f"""You are Jekyll, a WhatsApp-based calendar assistant. Classify the user's intent.

Current date: {datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")}
Current time: {datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")} PT

Return ONLY one word — no punctuation, no explanation.

CATEGORIES:
CREATE - adding any new event, appointment, reminder, or block
RECURRING - adding a repeating or regular event (contains: "every", "weekly", "daily", "each", "every week")
DELETE - canceling, removing, or deleting an existing event
LIST - asking what's on the calendar, what's coming up, any upcoming events
UNKNOWN - greetings, thanks, gibberish, unrelated

EXAMPLES:
"dentist Friday 3pm" → CREATE
"coffee with maya tmrw morning" → CREATE
"block off sunday afternoon" → CREATE
"remind me about the gym in 2 hours" → CREATE
"gym every tuesday 7am" → RECURRING
"weekly sync mondays at 10" → RECURRING
"daily standup 9am" → RECURRING
"cancel my dentist appt" → DELETE
"remove the thing friday" → DELETE
"delete my 3pm" → DELETE
"what do i have this week" → LIST
"anything tomorrow?" → LIST
"show my schedule" → LIST
"thanks" → UNKNOWN
"ok cool" → UNKNOWN
"lol" → UNKNOWN

Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip().upper()


def parse_event(message):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%H:%M")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    in_2h = (now + timedelta(hours=2)).strftime("%H:%M")
    in_30m = (now + timedelta(minutes=30)).strftime("%H:%M")

    prompt = f"""You are Jekyll, a WhatsApp-based calendar assistant. Parse the user's message into a calendar event.

Current date: {today} ({today_display})
Current time: {current_time} (Pacific Time)

Return ONLY a valid JSON object. No explanations, comments, markdown, or text outside the JSON.

OUTPUT SCHEMA:
{{
  "title": string,
  "date": "YYYY-MM-DD" | null,
  "time": "HH:MM" | null,
  "duration_minutes": integer | null,
  "location": string | null
}}

TITLE:
- Clean, concise, properly capitalized
- Preserve context ("Coffee with Maya", not "Coffee")
- Strip leading filler: "let's", "gonna", "need to", "i need to", "remember to", "don't forget to", "gotta", "have to", "want to"

DATE:
- "today" → {today}
- "tmrw" / "tomorrow" → {tomorrow}
- "next [weekday]" → upcoming occurrence, NEVER today
- "this [weekday]" → upcoming or same-day occurrence
- Bare weekday ("Monday", "Fri") → nearest future occurrence, not today
- "in N days" → today + N
- No date mentioned → null

TIME:
- "morning" → 09:00, "afternoon" → 14:00, "evening" → 18:00, "night" → 20:00, "noon" → 12:00, "eod" → 17:00
- "in 2 hours" → {in_2h}, "in 30 mins" → {in_30m} (round to nearest 5 min)
- Ambiguous bare number ("at 8", "at 7") — resolve by event type:
  - gym, run, yoga, breakfast, standup → AM
  - dinner, drinks, bar, party, movie → PM
  - meeting, call, sync, lunch → ≤7 assume PM, 8-11 assume AM, 12 = noon
- No time mentioned → null

DURATION (use explicit value if stated, otherwise):
- "quick" → 30, "long" / "extended" → 120
- coffee / chat / standup → 30
- lunch / dinner / brunch / drinks → 75
- doctor / dentist / checkup / therapy → 60
- gym / workout / run / yoga / hike → 60
- class / lecture / lab / workshop → 90
- meeting / sync / call / 1:1 → 45
- interview → 60
- haircut / barber / salon → 45
- movie → 120
- unknown type → 60

LOCATION:
- Only if explicitly mentioned (venue, address, "Zoom", "Google Meet")
- Never infer or hallucinate
- No mention → null

EXAMPLE:
"yo quick coffee with priya tmrw morning at blue bottle" →
{{"title": "Coffee with Priya", "date": "{tomorrow}", "time": "09:00", "duration_minutes": 30, "location": "Blue Bottle"}}

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

    prompt = f"""You are Jekyll, a WhatsApp-based calendar assistant. Extract the event the user wants to delete.

Current date: {today} ({today_display})

Return ONLY a valid JSON object. No explanations, comments, or markdown.

OUTPUT SCHEMA:
{{
  "title": string,
  "date": "YYYY-MM-DD" | null
}}

TITLE:
- Short and searchable — use the core event keyword(s) only
- Lowercase is fine, it will be used for fuzzy matching
- Examples: "dentist", "coffee with jake", "team sync", "3pm meeting"

DATE:
- Resolve relative references using today's date
- If no date mentioned → null

EXAMPLES:
"cancel my dentist friday" → {{"title": "dentist", "date": null}}
"remove the team sync tomorrow" → {{"title": "team sync", "date": null}}
"delete my 3pm today" → {{"title": "3pm", "date": "{today}"}}
"get rid of coffee with jake" → {{"title": "coffee with jake", "date": null}}

Message: "{message}" """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)


def parse_recurring(message):
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    today = now.strftime("%Y-%m-%d")
    today_display = now.strftime("%A, %B %d, %Y")
    current_time = now.strftime("%H:%M")

    prompt = f"""You are Jekyll, a WhatsApp-based calendar assistant. Parse a recurring event from the user's message.

Current date: {today} ({today_display})
Current time: {current_time} (Pacific Time)

Return ONLY a valid JSON object. No explanations, comments, or markdown.

OUTPUT SCHEMA:
{{
  "title": string,
  "time": "HH:MM" | null,
  "duration_minutes": integer | null,
  "recurrence": "DAILY" | "WEEKLY" | "MONTHLY",
  "day_of_week": "MO" | "TU" | "WE" | "TH" | "FR" | "SA" | "SU" | null
}}

TITLE:
- Clean, concise, properly capitalized
- Strip leading filler words

TIME:
- "morning" → 09:00, "afternoon" → 14:00, "evening" → 18:00, "night" → 20:00, "noon" → 12:00
- Ambiguous bare number → resolve by event type (same rules as one-off events)
- No time → null

RECURRENCE:
- "every day" / "daily" → DAILY, day_of_week = null
- "every [weekday]" / "weekly on [weekday]" → WEEKLY + set day_of_week
- "every week" with no day → WEEKLY, day_of_week = null
- "every month" / "monthly" → MONTHLY, day_of_week = null

DURATION:
- "quick" → 30, "long" → 120
- standup / scrum → 30
- gym / workout / run / yoga → 60
- class / lecture → 90
- meeting / sync → 45
- default → 60

EXAMPLES:
"gym every tuesday 7am" → {{"title": "Gym", "time": "07:00", "duration_minutes": 60, "recurrence": "WEEKLY", "day_of_week": "TU"}}
"daily standup at 9" → {{"title": "Standup", "time": "09:00", "duration_minutes": 30, "recurrence": "DAILY", "day_of_week": null}}
"weekly sync mondays at 10am for 30 mins" → {{"title": "Weekly Sync", "time": "10:00", "duration_minutes": 30, "recurrence": "WEEKLY", "day_of_week": "MO"}}
"monthly dentist checkup" → {{"title": "Dentist Checkup", "time": null, "duration_minutes": 60, "recurrence": "MONTHLY", "day_of_week": null}}

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
    if event_data.get("location"):
        event["location"] = event_data["location"]
    service.events().insert(calendarId="primary", body=event).execute()
    location_str = f" at {event_data['location']}" if event_data.get("location") else ""
    send_whatsapp(phone, f"Done ✅ {event_data['title']}{location_str} — {event_data['date']} at {event_data['time']}")

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
  .bg-glow-3 { position: fixed; width: 200px; height: 200px; border-radius: 50%; background: radial-gradient(circle, rgba(37,211,102,0.07) 0%, transparent 70%); top: 40%; left: 10%; pointer-events: none; }
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
  .connect-btn:active { transform: scale(0.99); }
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
  <div class="bg-glow-3"></div>
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
  .bg-glow-2 { position: fixed; width: 480px; height: 480px; border-radius: 50%; background: radial-gradient(circle, rgba(18,140,65,0.14) 0%, rgba(18,140,65,0.04) 45%, transparent 70%); bottom: -120px; left: -100px; pointer-events: none; }
  .bg-glow-3 { position: fixed; width: 200px; height: 200px; border-radius: 50%; background: radial-gradient(circle, rgba(37,211,102,0.08) 0%, transparent 70%); top: 40%; left: 10%; pointer-events: none; }
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
  .bubble { background: rgba(37,211,102,0.06); border: 0.5px solid rgba(37,211,102,0.15); border-radius: 12px 12px 12px 3px; padding: 10px 14px; font-size: 13px; color: rgba(232,228,222,0.7); animation: slideIn 0.4s cubic-bezier(0.22,1,0.36,1) both; }
  .bubble:nth-child(1) { animation-delay: 0.6s; }
  .bubble:nth-child(2) { animation-delay: 0.75s; }
  .bubble:nth-child(3) { animation-delay: 0.9s; }
  @keyframes slideIn { from { opacity: 0; transform: translateX(-10px); } to { opacity: 1; transform: translateX(0); } }
  .close-note { margin-top: 2rem; font-size: 12px; color: rgba(232,228,222,0.2); line-height: 1.6; }
</style>
</head>
<body>
  <div class="bg-grid"></div>
  <div class="bg-glow"></div>
  <div class="bg-glow-2"></div>
  <div class="bg-glow-3"></div>
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
      <div class="bubble">every Monday gym at 7am</div>
      <div class="bubble">what do I have this week?</div>
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
    return render_template_string(SUCCESS_PAGE)

if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8001)))