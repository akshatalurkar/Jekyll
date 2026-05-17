import sys
import time
from datetime import datetime, timedelta, timezone

from core import app, db, User, SentReminder, send_whatsapp
import calendar_ops

WINDOW_MINUTES = 5
DEFAULT_REMINDER_MINUTES = 30


def _format_reminder(title, start_dt, minutes):
    when = start_dt.astimezone(calendar_ops.TZ).strftime("%-I:%M%p").lower()
    lead = f"{minutes} min" if minutes < 60 else f"{minutes // 60} hr"
    return f"*{title}* starts at {when} — in {lead}."


def _events_for_user(user, service):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=25)
    out = []
    for cal in calendar_ops.get_user_calendars(user, service):
        try:
            items = service.events().list(
                calendarId=cal["id"],
                timeMin=now.isoformat(),
                timeMax=horizon.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute().get("items", [])
        except Exception:
            continue
        for e in items:
            if e.get("status") == "cancelled":
                continue
            dt_start = e["start"].get("dateTime")
            if not dt_start:
                continue
            out.append(e)
    return out


def _reminder_minutes(event):
    reminders = event.get("reminders", {})
    if reminders.get("useDefault"):
        return DEFAULT_REMINDER_MINUTES
    overrides = reminders.get("overrides", [])
    if not overrides:
        return None
    return min(o["minutes"] for o in overrides)


def process_user(user):
    if not user.oauth_token:
        return 0
    try:
        service = calendar_ops.get_service(user)
    except Exception as e:
        print(f"[reminders] service failed user={user.id}: {type(e).__name__}")
        return 0

    now = datetime.now(timezone.utc)
    sent = 0

    for event in _events_for_user(user, service):
        minutes = _reminder_minutes(event)
        if minutes is None:
            continue

        start_dt = datetime.fromisoformat(
            event["start"]["dateTime"].replace("Z", "+00:00")
        )
        fire_at = start_dt - timedelta(minutes=minutes)
        if not (now <= fire_at < now + timedelta(minutes=WINDOW_MINUTES)):
            continue

        event_id = event["id"]
        marker = f"{event_id}:{minutes}"
        if SentReminder.query.filter_by(user_id=user.id, event_id=marker).first():
            continue

        title = event.get("summary", "(untitled)")
        try:
            send_whatsapp(user.phone, _format_reminder(title, start_dt, minutes))
        except Exception as e:
            print(f"[reminders] send failed user={user.id} event={event_id}: {type(e).__name__}")
            continue

        db.session.add(SentReminder(user_id=user.id, event_id=marker))
        db.session.commit()
        sent += 1

    return sent


def run():
    with app.app_context():
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        SentReminder.query.filter(SentReminder.reminded_at < cutoff).delete()
        db.session.commit()

        total = 0
        for user in User.query.filter(User.oauth_token.isnot(None)).all():
            try:
                total += process_user(user)
            except Exception as e:
                print(f"[reminders] user={user.id} failed: {type(e).__name__}: {e}")
                db.session.rollback()
        print(f"[reminders] sent {total}")

if __name__ == "__main__":
    print("[reminders] worker started")
    while True:
        try:
            run()
        except Exception as e:
            print(f"[reminders] fatal: {type(e).__name__}: {e}")
        time.sleep(60)