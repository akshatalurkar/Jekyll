import time
from datetime import datetime, timedelta, timezone

from .core import app, db, User, SentReminder, send_whatsapp
from . import calendar_ops

LOOK_BACK_MINUTES = 2
LOOK_AHEAD_MINUTES = 1
DEFAULT_REMINDER_MINUTES = 30


def _format_reminder(title, start_dt, minutes):
    now = datetime.now(timezone.utc)
    actual_secs = (start_dt - now).total_seconds()
    when = start_dt.astimezone(calendar_ops.TZ).strftime("%-I:%M%p").lower()

    if actual_secs <= 0:
        return f"*{title}* is starting now."

    actual_mins = int(actual_secs / 60)
    if actual_mins < 60:
        lead = f"{actual_mins} min"
    elif actual_mins % 60 == 0:
        lead = f"{actual_mins // 60} hr"
    else:
        lead = f"{actual_mins // 60} hr {actual_mins % 60} min"

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
        window_start = now - timedelta(minutes=LOOK_BACK_MINUTES)
        window_end = now + timedelta(minutes=LOOK_AHEAD_MINUTES)
        if not (window_start <= fire_at <= window_end):
            continue

        event_id = event["id"]
        marker = f"{event_id}:{minutes}:{event['start']['dateTime']}"
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