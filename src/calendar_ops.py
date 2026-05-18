from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from .core import encrypt_token, decrypt_token, db, AuthExpiredError

import os

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
DEFAULT_REMINDER_MINUTES = 30
CALENDAR_LIST_TTL_HOURS = 24

PRIMARY_ALIASES = {
    "default", "primary", "main", "my calendar",
    "default calendar", "the default", "primary calendar",
}


def get_service(user):
    creds = Credentials(
        token=decrypt_token(user.oauth_token),
        refresh_token=decrypt_token(user.refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            user.oauth_token = encrypt_token(creds.token)
            db.session.commit()
        except Exception:
            raise AuthExpiredError()
    return build("calendar", "v3", credentials=creds)


def get_user_calendars(user, service):
    stale = (
        not user.calendars
        or not user.calendars_updated_at
        or datetime.now(timezone.utc) - user.calendars_updated_at.replace(tzinfo=timezone.utc)
            > timedelta(hours=CALENDAR_LIST_TTL_HOURS)
    )
    if stale:
        result = service.calendarList().list().execute()
        user.calendars = [
            {"id": c["id"], "name": c["summary"]}
            for c in result.get("items", [])
            if c.get("accessRole") in ("owner", "writer")
        ]
        user.calendars_updated_at = datetime.now(timezone.utc)
        db.session.commit()
    return user.calendars


def _calendar_similarity(hint, name):
    h, n = hint.lower(), name.lower()
    if h in n or n in h:
        return 1.0
    return SequenceMatcher(None, h, n).ratio()


def resolve_calendar(user, service, hint):
    if not hint or not hint.strip():
        return "primary", "Default"
    if hint.strip().lower() in PRIMARY_ALIASES:
        return "primary", "Default"
    calendars = get_user_calendars(user, service)
    if not calendars:
        return "primary", "Default"
    best = max(calendars, key=lambda c: _calendar_similarity(hint, c["name"]))
    if _calendar_similarity(hint, best["name"]) >= 0.75:
        return best["id"], best["name"]
    return None


def _to_pacific(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(TZ)


def _classify_overlap(new_start, new_end, ev_start, ev_end):
    if ev_start == new_start and ev_end == new_end:
        return "exact"
    if ev_start <= new_start and ev_end >= new_end:
        return "contains"
    if ev_start >= new_start and ev_end <= new_end:
        return "contained"
    if ev_start < new_start:
        return "overlaps_start"
    return "overlaps_end"


def find_conflicts(user, service, date, time, duration_minutes, exclude_event_id=None):
    new_start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    new_end = new_start + timedelta(minutes=duration_minutes or DEFAULT_DURATION_MINUTES)

    conflicts = []
    for cal in get_user_calendars(user, service):
        try:
            items = service.events().list(
                calendarId=cal["id"],
                timeMin=new_start.isoformat(),
                timeMax=new_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])
        except Exception:
            continue

        for e in items:
            if exclude_event_id and e.get("id") == exclude_event_id:
                continue
            if e.get("status") == "cancelled":
                continue
            if e.get("transparency") == "transparent":
                continue

            dt_start = e["start"].get("dateTime")
            all_day = dt_start is None
            if all_day:
                ev_start = datetime.strptime(e["start"]["date"], "%Y-%m-%d").replace(tzinfo=TZ)
                ev_end = datetime.strptime(e["end"]["date"], "%Y-%m-%d").replace(tzinfo=TZ)
            else:
                ev_start = _to_pacific(dt_start)
                ev_end = _to_pacific(e["end"]["dateTime"])

            conflicts.append({
                "title": e.get("summary", "(untitled)"),
                "calendar_name": cal["name"],
                "start_dt": ev_start,
                "end_dt": ev_end,
                "overlap": _classify_overlap(new_start, new_end, ev_start, ev_end),
                "all_day": all_day,
            })

    conflicts.sort(key=lambda c: c["start_dt"])
    return conflicts


def find_matching_events(user, service, keyword, max_results=50):
    if not keyword:
        return []

    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).isoformat()

    keywords = [keyword.lower()]
    words = keyword.lower().split()
    if len(words) > 1:
        keywords += [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]

    matches = []
    seen_ids = set()

    for cal in get_user_calendars(user, service):
        try:
            events = service.events().list(
                calendarId=cal["id"],
                timeMin=today_start_utc,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])
        except Exception:
            continue

        for ev in events:
            ev_id = ev.get("id")
            if ev_id in seen_ids:
                continue
            title = ev.get("summary", "").lower()
            if any(kw in title for kw in keywords):
                ev["_calendar_id"] = cal["id"]
                ev["_calendar_name"] = cal["name"]
                matches.append(ev)
                seen_ids.add(ev_id)

    return matches


def list_events_for_day(user, service, day_iso):
    day = datetime.strptime(day_iso, "%Y-%m-%d").date()
    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=TZ)
    end = start + timedelta(days=1)

    out = []
    for cal in get_user_calendars(user, service):
        try:
            events = service.events().list(
                calendarId=cal["id"],
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=50,
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])
            for ev in events:
                ev["_calendar_name"] = cal["name"]
                out.append(ev)
        except Exception:
            continue
    return out


def _reminders_body(reminder_minutes):
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": reminder_minutes or DEFAULT_REMINDER_MINUTES}
        ],
    }


def insert_event(service, calendar_id, title, date, time,
                 duration_minutes, location, reminder_minutes):
    start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    end = start + timedelta(minutes=duration_minutes or DEFAULT_DURATION_MINUTES)
    body = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
        "reminders": _reminders_body(reminder_minutes),
    }
    if location:
        body["location"] = location
    return service.events().insert(calendarId=calendar_id, body=body).execute()


def patch_event(service, calendar_id, event_id, fields):
    body = {}
    if "title" in fields:
        body["summary"] = fields["title"]

    if any(k in fields for k in ("date", "time", "duration_minutes")):
        d = fields["date"]
        t = fields["time"]
        dur = fields.get("duration_minutes") or DEFAULT_DURATION_MINUTES
        start = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        end = start + timedelta(minutes=dur)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"}

    if "location" in fields:
        body["location"] = fields["location"]

    if "reminder_minutes" in fields:
        body["reminders"] = _reminders_body(fields["reminder_minutes"])

    return service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()


def delete_event(service, calendar_id, event_id):
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()