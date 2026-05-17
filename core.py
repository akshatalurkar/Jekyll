import hashlib
import hmac
import os
import requests
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

if os.getenv("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

database_url = os.getenv("DATABASE_URL", "sqlite:///users.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

REQUIRED_ENV = [
    "FLASK_SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "GEMINI_API_KEY",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
]
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    oauth_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)
    last_event = db.Column(db.JSON, nullable=True)
    last_event_updated_at = db.Column(db.DateTime, nullable=True)
    calendars = db.Column(db.JSON, nullable=True)
    calendars_updated_at = db.Column(db.DateTime, nullable=True)


class ProcessedMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class SentReminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    event_id = db.Column(db.String(255), nullable=False)
    reminded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("user_id", "event_id", name="uq_user_event_reminder"),)

with app.app_context():
    db.create_all()

FERNET = Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())

def encrypt_token(plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    return FERNET.encrypt(plaintext.encode()).decode()

def decrypt_token(ciphertext: str | None) -> str | None:
    if ciphertext is None:
        return None
    return FERNET.decrypt(ciphertext.encode()).decode()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")
NOTION_URL = "https://www.notion.so/User-guide-for-Jekyll-35d2b89f0b3980faa54eccbd930609c0?source=copy_link"

def normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone

def send_whatsapp(to: str, text: str) -> dict:
    response = requests.post(
        f"https://graph.facebook.com/v18.0/{os.getenv('WHATSAPP_PHONE_NUMBER_ID')}/messages",
        headers={
            "Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
    )
    return response.json()

def verify_whatsapp_signature(req) -> bool:
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        os.environ["WHATSAPP_APP_SECRET"].encode(),
        req.get_data(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature[7:], expected)