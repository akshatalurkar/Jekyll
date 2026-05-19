from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .core import encrypt_token, decrypt_token, db, AuthExpiredError

import os

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
DEFAULT_REMINDER_MINUTES = 30
CALENDAR_LIST_TTL_HOURS = 24
# Refresh access token a little before Google's 60-min expiry to avoid
# racing the boundary on long-running requests.
TOKEN_REFRESH_SKEW_SECONDS = 120

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
    # Only refresh when the current access token is missing, expired, or about
    # to expire — refreshing on every call adds ~200–500ms latency and burns
    # through Google's refresh-rate budget. `creds.expired` returns True when
    # `expiry` is set and in the past; if `expiry` is unknown (None) we treat
    # the token as needing a refresh so the very first call still works.
    needs_refresh = (
        not creds.token
        or creds.expiry is None
        or creds.expired
        or (creds.expiry - datetime.utcnow()).total_seconds() < TOKEN_REFRESH_SKEW_SECONDS
    )
    if needs_refresh and creds.refresh_token:
        try:
            creds.refresh(Request())
            user.oauth_token = encrypt_token(creds.token)
            db.session.commit()
        except RefreshError:
            raise AuthExpiredError()
    try:
        return build("calendar", "v3", credentials=creds)
    except RefreshError:
        raise AuthExpiredError()
    except HttpError as e:
        if getattr(e, "status_code", None) == 401 or getattr(getattr(e, "resp", None), "status", None) == 401:
            raise AuthExpiredError()
        raise


def get_user_calendars(user, service):
    stale = (
        not user.calendars
        or not user.calendars_updated_at
        or datetime.now(timezone.utc) - (user.calendars_updated_at if user.calendars_updated_at.tzinfo else user.calendars_updated_at.replace(tzinfo=timezone.utc))
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
    if not keyword or not keyword.strip():
        return []

    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_utc = (today_start - timedelta(days=2)).astimezone(timezone.utc).isoformat()
    window_end_utc = (today_start + timedelta(days=60)).astimezone(timezone.utc).isoformat()

    keywords = [keyword.lower()]
    words = keyword.lower().split()
    if len(words) > 1:
        keywords += [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]

    # Server-side pre-filter — pick the longest word as it's most likely to be
    # the distinctive token (e.g. "lunch with maya" → "maya" is more selective
    # than "lunch"). Guarded against empty `words` from whitespace-only input
    # (already filtered above, but defensive in case of unicode oddities).
    q_term = max(words, key=len) if words else keyword.lower()

    matches = []
    seen_ids = set()

    for cal in get_user_calendars(user, service):
        try:
            events = service.events().list(
                calendarId=cal["id"],
                timeMin=window_start_utc,
                timeMax=window_end_utc,
                q=q_term,
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
    mins = reminder_minutes or DEFAULT_REMINDER_MINUTES
    if mins > 40320:
        mins = 40320
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": mins}
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
    }
    if reminder_minutes is not None:
        body["reminders"] = _reminders_body(reminder_minutes)
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