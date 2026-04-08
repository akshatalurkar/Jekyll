import os
import sys
import json
import secrets
import hashlib
import base64
from datetime import date
from flask import Flask, request, session
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from google import genai
from requests_oauthlib import OAuth2Session
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from twilio.rest import Client as TwilioClient

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///users.db"
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    oauth_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)

with app.app_context():
    db.create_all()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def parse_event(message):
    today = date.today().strftime("%A, %B %d, %Y")
    prompt = f"""Today is {today}.
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

def create_calendar_event(user, event_data):
    creds = Credentials(
        token=user.oauth_token,
        refresh_token=user.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    )
    service = build("calendar", "v3", credentials=creds)
    start = datetime.strptime(
        f"{event_data['date']} {event_data['time']}", "%Y-%m-%d %H:%M"
    )
    end = start + timedelta(minutes=event_data["duration_minutes"] or 60)
    event = {
        "summary": event_data["title"],
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
    }
    return service.events().insert(calendarId="primary", body=event).execute()

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From")
    message = request.form.get("Body")

    twilio_client = TwilioClient(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN")
    )

    user = User.query.filter_by(phone=phone).first()

    if not user or not user.oauth_token:
        twilio_client.messages.create(
            to=phone,
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            body=f"Welcome! Connect your Google Calendar first: http://localhost:8000/auth/{phone}"
        )
        return "OK", 200

    try:
        event_data = parse_event(message)
        create_calendar_event(user, event_data)
        confirmation = f"Added: {event_data['title']} on {event_data['date']} at {event_data['time']}"
    except Exception as e:
        print(f"Error: {e}")
        confirmation = "Something went wrong — please try again in a moment."

    twilio_client.messages.create(
        to=phone,
        from_=os.getenv("TWILIO_PHONE_NUMBER"),
        body=confirmation
    )
    return "OK", 200

@app.route("/auth/<phone>")
def auth(phone):
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    oauth = OAuth2Session(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        redirect_uri="http://localhost:8000/oauth/callback",
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
        redirect_uri="http://localhost:8000/oauth/callback",
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
    app.run(debug=True, port=8000)