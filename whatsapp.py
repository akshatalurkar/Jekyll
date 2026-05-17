import traceback
from datetime import datetime, timedelta, timezone
from models import CalendarAction
from flask import request, session, render_template
from requests_oauthlib import OAuth2Session
import base64
import hashlib
import secrets
import os

TEST_MODE = os.getenv("JEKYLL_TEST_MODE") == "1"

from core import (
    app, db, User, ProcessedMessage, SentReminder,
    send_whatsapp, verify_whatsapp_signature,
    encrypt_token, normalize_phone,
    BASE_URL, NOTION_URL,
)
import state
import parse
import patch


@app.route("/auth/<phone>")
def auth(phone):
    phone = normalize_phone(phone)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    oauth = OAuth2Session(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        redirect_uri=f"{BASE_URL}/oauth/callback",
        scope=["https://www.googleapis.com/auth/calendar"],
    )
    auth_url, oauth_state = oauth.authorization_url(
        "https://accounts.google.com/o/oauth2/auth",
        access_type="offline",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    session["state"] = oauth_state
    session["phone"] = phone
    session["code_verifier"] = code_verifier
    return render_template("auth.html", phone=phone, auth_url=auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    phone = session.get("phone")
    code_verifier = session.get("code_verifier")
    oauth = OAuth2Session(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        redirect_uri=f"{BASE_URL}/oauth/callback",
        state=session.get("state"),
    )
    token = oauth.fetch_token(
        "https://oauth2.googleapis.com/token",
        authorization_response=request.url,
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        code_verifier=code_verifier,
    )
    user = User.query.filter_by(phone=phone).first()
    if not user:
        user = User(phone=phone)
        db.session.add(user)
    user.oauth_token = encrypt_token(token["access_token"])
    user.refresh_token = encrypt_token(token.get("refresh_token"))
    db.session.commit()
    return render_template("success.html")


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
            return challenge, 200
        return "Forbidden", 403

    if not verify_whatsapp_signature(request):
        return "Forbidden", 403

    body = request.json
    try:
        msg = body["entry"][0]["changes"][0]["value"]["messages"][0]
        phone = normalize_phone(msg["from"])
        text = msg["text"]["body"].strip()
        message_id = msg["id"]
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    if ProcessedMessage.query.filter_by(message_id=message_id).first():
        return "OK", 200
    db.session.add(ProcessedMessage(message_id=message_id))
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    ProcessedMessage.query.filter(ProcessedMessage.created_at < cutoff).delete()
    db.session.commit()

    user = User.query.filter_by(phone=phone).first()
    if not user or not user.oauth_token:
        send_whatsapp(
            phone,
            f"Welcome to Jekyll!\n\n"
            f"Connect your Google Calendar: {BASE_URL}/auth/{phone}\n\n"
            f"Once connected, text what you'd like to schedule.\n"
            f"Examples: 'Dentist Friday 3pm', 'What do I have today?'\n\n"
            f"Full guide: {NOTION_URL}",
        )
        return "OK", 200

    try:
        if state.is_stale(user):
            state.clear_pending(db, user)
        pending = state.get_pending(user)
        lowered = text.strip().lower()
        YES = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "do it", "confirm", "correct", "y"}
        NO = {"no", "nope", "never mind", "nevermind", "cancel", "stop", "forget it", "n"}
        if pending and lowered in YES:
            action = CalendarAction(action="confirm")
        elif pending and lowered in NO:
            action = CalendarAction(action="cancel")
        else:
            action = parse.parse(text, pending)
        reply = patch.dispatch(db, user, action, message=text)
        if TEST_MODE:
            return reply, 200
        send_whatsapp(user.phone, reply)
        return "", 200
    except Exception as e:
        traceback.print_exc()
        try:
            state.clear_pending(db, user)
        except Exception:
            pass
        try:
            msg = "Something went wrong. Try again in a moment."
            if TEST_MODE:
                return msg, 500
            send_whatsapp(phone, msg)
        except Exception:
            pass
        return "OK", 500


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 8001)))