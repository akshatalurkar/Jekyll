"""
The orchestration brain.

Every action from parser.py routes through dispatch(). State for
pending creates/updates/deletes lives in the DB (User.last_event).

Design:
- Universal actions (reject, clarify, confirm, cancel) handled first.
- 'create' with pending state present = correction. Merge and re-display.
- 'create' with no pending = fresh create. Detect past/now/conflict, set pending, ask for confirm.
- 'update' = find event, set pending with diff, ask for confirm.
- 'delete' = find event, set pending with details, ask for confirm.
- 'list' = bound to today/tomorrow/yesterday; reject otherwise.
- 'detail' = find event in next 7 days, return full info.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import formatted
import state
import calendar_ops
from models import CalendarAction, EventFields

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
LIST_SCOPE_DAYS = 1   

def dispatch(db, user, action: CalendarAction) -> str:
    pending = state.get_pending(user)

    if action.action == "reject":
        return formatted.rejected()

    if action.action == "clarify":
        return formatted.clarify(action.clarification)

    if action.action == "confirm":
        return _confirm(db, user, pending)

    if action.action == "cancel":
        return _cancel(db, user, pending)

    if pending and action.action == "create":
        return _correction(db, user, pending, action.event)

    if action.action == "create":
        return _create(db, user, action.event)

    if action.action == "update":
        return _update(db, user, action.target_query, action.event)

    if action.action == "delete":
        return _delete(db, user, action.target_query)

    if action.action == "list":
        return _list(user, action.list_date)

    if action.action == "detail":
        return _detail(user, action.target_query)

    if action.action == "list_calendars":
        return _list_calendars(user)

    return formatted.error()

#confirm

def _confirm(db, user, pending: dict | None) -> str:
    if not pending:
        return formatted.nothing_pending()
    if state.is_stale(user):
        state.clear_pending(db, user)
        return formatted.pending_timed_out()

    kind = pending.get("kind")
    try:
        if kind == "create":
            return _execute_create(db, user, pending["event"])
        if kind == "update":
            return _execute_update(db, user, pending)
        if kind == "delete":
            return _execute_delete(db, user, pending)
    except Exception:
        state.clear_pending(db, user)
        return formatted.error()

    state.clear_pending(db, user)
    return formatted.error()


def _cancel(db, user, pending: dict | None) -> str:
    if not pending:
        return formatted.nothing_pending()
    kind = pending.get("kind")
    state.clear_pending(db, user)
    if kind == "delete":
        return formatted.cancelled_delete()
    if kind == "update":
        return formatted.cancelled_update()
    return formatted.cancelled_create()

#create

def _create(db, user, event_fields: EventFields | None) -> str:
    if not event_fields or not event_fields.title:
        return formatted.clarify("What's the event?")
    if not event_fields.date or not event_fields.time:
        return formatted.clarify("What date and time?")

    service = calendar_ops.get_service(user)
    resolved = calendar_ops.resolve_calendar(user, service, event_fields.calendar)
    if resolved is None:
        return formatted.clarify(
            f"Couldn't find a calendar matching '{event_fields.calendar}'. "
            f"Text 'what calendars do I have' to see your exact calendar names."
        )
    cal_id, cal_name = resolved

    event = {
        "title": event_fields.title,
        "date": event_fields.date,
        "time": event_fields.time,
        "duration_minutes": event_fields.duration_minutes,
        "location": event_fields.location,
        "calendar_id": cal_id,
        "calendar_name": cal_name,
    }

    warning, conflict = _detect_warning(user, service, event)
    payload = {"kind": "create", "event": event, "warning": warning}
    if conflict:
        payload["conflict"] = conflict

    state.set_pending(db, user, payload)
    return formatted.create_confirmation(event, warning, conflict)


def _correction(db, user, pending: dict, event_fields: EventFields | None) -> str:
    """Merge user's correction into the pending event and re-display."""
    if not event_fields:
        return formatted.clarify("What would you like to change?")

    kind = pending.get("kind")

    if kind == "delete":
       
        return formatted.clarify("Reply *Yes* to confirm the deletion or *No* to cancel.")

    current = pending.get("event", {})
    merged = {**current}
    for k, v in event_fields.model_dump(exclude_none=True).items():
        merged[k] = v

    
    if event_fields.calendar:
        service = calendar_ops.get_service(user)
        cal_id, cal_name = calendar_ops.resolve_calendar(user, service, event_fields.calendar)
        merged["calendar_id"] = cal_id
        merged["calendar_name"] = cal_name

    if kind == "create":
        service = calendar_ops.get_service(user)
        warning, conflict = _detect_warning(user, service, merged)
        payload = {"kind": "create", "event": merged, "warning": warning}
        if conflict:
            payload["conflict"] = conflict
        state.set_pending(db, user, payload)
        return formatted.create_confirmation(merged, warning, conflict)

    if kind == "update":
        
        payload = {
            "kind": "update",
            "event_id": pending["event_id"],
            "event": merged,
            "original": pending["original"],
        }
        state.set_pending(db, user, payload)
        diff_lines = _build_update_diff(pending["original"], merged)
        return formatted.update_confirmation(pending["original"]["title"], diff_lines)

    return formatted.error()


def _detect_warning(user, service, event: dict) -> tuple[str | None, dict | None]:
    """Returns (warning_kind, conflict_info)."""
    now = datetime.now(TZ)
    event_dt = datetime.strptime(
        f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)

    if event_dt < now:
        return "past", None
    if event_dt <= now + timedelta(minutes=5):
        return "now", None

    conflicts = calendar_ops.find_conflicts(
        user, service, event["date"], event["time"], event.get("duration_minutes")
    )
    if conflicts:
        c = conflicts[0]
        c_start = c["start"].get("dateTime") or c["start"].get("date")
        c_dt = datetime.fromisoformat(c_start.replace("Z", "+00:00")).astimezone(TZ)
        return "conflict", {
            "title": c.get("summary", "(untitled)"),
            "time": c_dt.strftime("%a %b %-d at %-I:%M%p").lower(),
        }
    return None, None


def _execute_create(db, user, event: dict) -> str:
    service = calendar_ops.get_service(user)
    calendar_ops.insert_event(
        service,
        calendar_id=event["calendar_id"],
        title=event["title"],
        date=event["date"],
        time=event["time"],
        duration_minutes=event.get("duration_minutes"),
        location=event.get("location"),
    )
    state.clear_pending(db, user)
    return formatted.create_success(event)

# update

def _update(db, user, target_query: str | None, changes: EventFields | None) -> str:
    if not target_query:
        return formatted.clarify("Which event?")
    if not changes or not changes.model_dump(exclude_none=True):
        return formatted.clarify("What would you like to change?")

    service = calendar_ops.get_service(user)
    match = calendar_ops.find_upcoming_event(user, service, target_query)
    if not match:
        return formatted.not_found(target_query)

    original = _event_to_dict(match)

    new_cal_id = original["calendar_id"]
    new_cal_name = original["calendar_name"]
    if changes.calendar:
        resolved = calendar_ops.resolve_calendar(user, service, changes.calendar)
        if resolved is None:
            return formatted.clarify(
                f"Couldn't find a calendar matching '{changes.calendar}'. "
                f"Text 'what calendars do I have' to see your exact calendar names."
            )
        new_cal_id, new_cal_name = resolved

    new_event = {**original}
    for k, v in changes.model_dump(exclude_none=True).items():
        if k == "calendar":
            continue
        new_event[k] = v
    new_event["calendar_id"] = new_cal_id
    new_event["calendar_name"] = new_cal_name

    payload = {
        "kind": "update",
        "event_id": match["id"],
        "event": new_event,
        "original": original,
    }
    state.set_pending(db, user, payload)

    diff_lines = _build_update_diff(original, new_event)
    return formatted.update_confirmation(original["title"], diff_lines)


def _build_update_diff(original: dict, new_event: dict) -> list[str]:
    """One line per field that changed."""
    lines = []
    if original["title"] != new_event["title"]:
        lines.append(f"Title: {original['title']} → {new_event['title']}")
    if original["date"] != new_event["date"]:
        lines.append(f"Date: {original['date']} → {new_event['date']}")
    if original["time"] != new_event["time"]:
        lines.append(f"Time: {formatted.fmt_time(original['time'])} → {formatted.fmt_time(new_event['time'])}")
    if (original.get("duration_minutes") or DEFAULT_DURATION_MINUTES) != (new_event.get("duration_minutes") or DEFAULT_DURATION_MINUTES):
        lines.append(f"Duration: {original.get('duration_minutes') or DEFAULT_DURATION_MINUTES} → {new_event.get('duration_minutes') or DEFAULT_DURATION_MINUTES} min")
    if (original.get("location") or "") != (new_event.get("location") or ""):
        new_loc = new_event.get("location") or "(cleared)"
        old_loc = original.get("location") or "(none)"
        lines.append(f"Location: {old_loc} → {new_loc}")
    if original["calendar_id"] != new_event["calendar_id"]:
        lines.append(f"Calendar: {original['calendar_name']} → {new_event['calendar_name']}")
    return lines or ["(no changes)"]


def _execute_update(db, user, pending: dict) -> str:
    service = calendar_ops.get_service(user)
    new_event = pending["event"]
    original = pending["original"]

    if new_event["calendar_id"] != original["calendar_id"]:
        calendar_ops.delete_event(service, original["calendar_id"], pending["event_id"])
        calendar_ops.insert_event(
            service,
            calendar_id=new_event["calendar_id"],
            title=new_event["title"],
            date=new_event["date"],
            time=new_event["time"],
            duration_minutes=new_event.get("duration_minutes"),
            location=new_event.get("location"),
        )
    else:
        fields_to_patch = {}
        if new_event["title"] != original["title"]:
            fields_to_patch["title"] = new_event["title"]
        if (new_event["date"] != original["date"]
                or new_event["time"] != original["time"]
                or new_event.get("duration_minutes") != original.get("duration_minutes")):
            fields_to_patch["date"] = new_event["date"]
            fields_to_patch["time"] = new_event["time"]
            fields_to_patch["duration_minutes"] = new_event.get("duration_minutes") or DEFAULT_DURATION_MINUTES
        if (new_event.get("location") or "") != (original.get("location") or ""):
            fields_to_patch["location"] = new_event.get("location") or ""

        if fields_to_patch:
            calendar_ops.patch_event(service, new_event["calendar_id"], pending["event_id"], fields_to_patch)

    state.clear_pending(db, user)
    return formatted.update_success(new_event["title"])

#delete

def _delete(db, user, target_query: str | None) -> str:
    if not target_query:
        return formatted.clarify("Which event should I remove?")

    service = calendar_ops.get_service(user)
    match = calendar_ops.find_upcoming_event(user, service, target_query)
    if not match:
        return formatted.not_found(target_query)

    start = match["start"].get("dateTime") or match["start"].get("date")
    dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
    when = dt.strftime("%a %b %-d at %-I:%M%p").lower()

    payload = {
        "kind": "delete",
        "event_id": match["id"],
        "calendar_id": match["_calendar_id"],
        "title": match.get("summary", "(untitled)"),
        "when": when,
    }
    state.set_pending(db, user, payload)
    return formatted.delete_confirmation(payload["title"], when)


def _execute_delete(db, user, pending: dict) -> str:
    service = calendar_ops.get_service(user)
    calendar_ops.delete_event(service, pending["calendar_id"], pending["event_id"])
    state.clear_pending(db, user)
    return formatted.delete_success(pending["title"])

#list

def _list(user, list_date: str | None) -> str:
    if not list_date:
        return formatted.clarify("Which day? Today, tomorrow, or yesterday?")

    today = datetime.now(TZ).date()
    requested = datetime.strptime(list_date, "%Y-%m-%d").date()
    delta = (requested - today).days

    if abs(delta) > LIST_SCOPE_DAYS:
        return formatted.list_out_of_scope()

    label = {0: "today", 1: "tomorrow", -1: "yesterday"}[delta]
    date_label = requested.strftime("%a %b %-d")

    service = calendar_ops.get_service(user)
    events = calendar_ops.list_events_for_day(user, service, list_date)
    events.sort(key=lambda e: e["start"].get("dateTime", e["start"].get("date", "")))

    return formatted.list_grouped(label, date_label, events)

#detail

def _detail(user, target_query: str | None) -> str:
    if not target_query:
        return formatted.clarify("Which event?")

    service = calendar_ops.get_service(user)
    match = calendar_ops.find_upcoming_event(user, service, target_query)
    if not match:
        return formatted.not_found(target_query)
    return formatted.event_detail(match)

#list calendars

def _list_calendars(user) -> str:
    service = calendar_ops.get_service(user)
    calendars = calendar_ops.get_user_calendars(user, service)
    return formatted.calendar_names([c["name"] for c in calendars])

#helper method

def _event_to_dict(gcal_event: dict) -> dict:
    """Convert a Google Calendar event into our internal event dict."""
    start = gcal_event["start"].get("dateTime") or gcal_event["start"].get("date")
    end = gcal_event["end"].get("dateTime") or gcal_event["end"].get("date")
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(TZ)
    return {
        "title": gcal_event.get("summary", "(untitled)"),
        "date": start_dt.strftime("%Y-%m-%d"),
        "time": start_dt.strftime("%H:%M"),
        "duration_minutes": int((end_dt - start_dt).total_seconds() / 60),
        "location": gcal_event.get("location") or "",
        "calendar_id": gcal_event["_calendar_id"],
        "calendar_name": gcal_event["_calendar_name"],
    }