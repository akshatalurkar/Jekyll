import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import formatted
import state
import calendar_ops
from models import CalendarAction, EventFields

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
DEFAULT_REMINDER_MINUTES = 30
LIST_SCOPE_DAYS = 1

_CAL_TO_X = re.compile(r"calendars?\s+to\s+(.+)", re.I)
_TO_X_CAL = re.compile(r"\bto\s+(?:the\s+|my\s+)?(.+?)\s+calendars?\b", re.I)


def _calendar_from_text(message):
    if not message:
        return None
    m = _CAL_TO_X.search(message)
    if m:
        return m.group(1).strip(" .")
    m = _TO_X_CAL.search(message)
    if m:
        return m.group(1).strip(" .")
    return None

def _resolve_reminder(reminder_at, event_date, event_time):
    if not reminder_at:
        return None
    start = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
    rem = datetime.strptime(f"{event_date} {reminder_at}", "%Y-%m-%d %H:%M")
    minutes = int((start - rem).total_seconds() / 60)
    return minutes

def dispatch(db, user, action: CalendarAction, message: str = "") -> str:
    pending = state.get_pending(user)

    if action.action == "reject":
        return formatted.rejected()

    if action.action == "clarify":
        return formatted.clarify(action.clarification)

    if action.action == "confirm":
        return _confirm(db, user, pending)

    if action.action == "cancel":
        return _cancel(db, user, pending)

    if action.action == "refresh":
        return _refresh(db, user)

    if pending and action.action == "create":
        if pending.get("kind") == "create" and action.event and action.event.title:
            state.clear_pending(db, user)
            return _create(db, user, action.event, message)
        return _correction(db, user, pending, action.event, message)

    if action.action == "create":
        return _create(db, user, action.event, message)

    if action.action == "update":
        if pending and pending.get("kind") == "create":
            return _correction(db, user, pending, action.event, message)
        return _update(db, user, action.target_query, action.event, message)

    if action.action == "delete":
        return _delete(db, user, action.target_query)

    if action.action == "list":
        return _list(user, action.list_date)

    if action.action == "detail":
        return _detail(user, action.target_query)

    if action.action == "list_calendars":
        return _list_calendars(user)

    return formatted.error()


def _refresh(db, user):
    user.calendars = None
    user.calendars_updated_at = None
    db.session.commit()
    service = calendar_ops.get_service(user)
    calendar_ops.get_user_calendars(user, service)
    return "Refreshed."


def _confirm(db, user, pending):
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
        state.clear_pending(db, user)
        return formatted.error()
    except Exception as e:
        print(f"[confirm error] kind={kind} error={type(e).__name__}: {e}")
        state.clear_pending(db, user)
        return "Something went wrong and your calendar wasn't updated. Try again."


def _cancel(db, user, pending):
    if not pending:
        return formatted.nothing_pending()
    kind = pending.get("kind")
    state.clear_pending(db, user)
    if kind == "delete":
        return formatted.cancelled_delete()
    if kind == "update":
        return formatted.cancelled_update()
    return formatted.cancelled_create()


def _create(db, user, event_fields, message=""):
    if not event_fields or not event_fields.title:
        return formatted.clarify("What's the event?")

    cal_hint = event_fields.calendar
    if not (cal_hint and cal_hint.strip()):
        cal_hint = _calendar_from_text(message)

    service = calendar_ops.get_service(user)
    resolved = calendar_ops.resolve_calendar(user, service, cal_hint)
    if resolved is None:
        return formatted.clarify(
            f"Couldn't find a calendar matching '{cal_hint}'. "
            f"Text 'what calendars do I have' to see your options."
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
        "reminder_minutes": event_fields.reminder_minutes,
    }

    if not event_fields.date or not event_fields.time:
        state.set_pending(db, user, {"kind": "create", "event": event, "warning": None})
        return formatted.clarify("What date and time?")

    if not event.get("reminder_minutes") and event_fields.reminder_at:
        event["reminder_minutes"] = _resolve_reminder(event_fields.reminder_at, event["date"], event["time"])

    warning, conflicts = _detect_warning(user, service, event)
    payload = {"kind": "create", "event": event, "warning": warning}
    if conflicts:
        payload["conflicts"] = conflicts

    state.set_pending(db, user, payload)
    return formatted.create_confirmation(event, warning, conflicts)


def _correction(db, user, pending, event_fields, message=""):
    if not event_fields:
        return formatted.clarify("What would you like to change?")

    kind = pending.get("kind")
    if kind == "delete":
        return formatted.clarify("Reply *Yes* to confirm the deletion or *No* to cancel.")

    current = pending.get("event", {})
    merged = {**current}
    for k, v in event_fields.model_dump(exclude_none=True).items():
        merged[k] = v

    cal_hint = event_fields.calendar
    if not (cal_hint and cal_hint.strip()):
        cal_hint = _calendar_from_text(message)

    if cal_hint and cal_hint.strip():
        service = calendar_ops.get_service(user)
        resolved = calendar_ops.resolve_calendar(user, service, cal_hint)
        if resolved is None:
            return formatted.clarify(
                f"Couldn't find a calendar matching '{cal_hint}'. "
                f"Text 'what calendars do I have' to see your options."
            )
        merged["calendar_id"], merged["calendar_name"] = resolved
    merged.pop("calendar", None)

    if merged == current:
        return formatted.clarify(
            "I didn't catch a change. You can adjust the time, date, "
            "calendar, location, duration, or reminder."
        )

    if kind == "create":
        if not merged.get("date") or not merged.get("time"):
            state.set_pending(db, user, {"kind": "create", "event": merged, "warning": None})
            return formatted.clarify("What date and time?")
        
        if not merged.get("reminder_minutes") and event_fields.reminder_at:
            merged["reminder_minutes"] = _resolve_reminder(event_fields.reminder_at, merged["date"], merged["time"])

        service = calendar_ops.get_service(user)
        warning, conflicts = _detect_warning(user, service, merged)
        payload = {"kind": "create", "event": merged, "warning": warning}
        if conflicts:
            payload["conflicts"] = conflicts
        state.set_pending(db, user, payload)
        return formatted.create_confirmation(merged, warning, conflicts)

    if kind == "update":
        payload = {
            "kind": "update",
            "event_id": pending["event_id"],
            "event": merged,
            "original": pending["original"],
        }
        state.set_pending(db, user, payload)
        diff_lines = _build_update_diff(pending["original"], merged)
        return formatted.update_confirmation(pending["original"]["title"], diff_lines, None)

    return formatted.error()


def _detect_warning(user, service, event, exclude_event_id=None):
    now = datetime.now(TZ)
    event_dt = datetime.strptime(
        f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)

    if event_dt < now:
        return "past", None
    if event_dt <= now + timedelta(minutes=5):
        return "now", None

    raw = calendar_ops.find_conflicts(
        user, service,
        event["date"], event["time"], event.get("duration_minutes"),
        exclude_event_id=exclude_event_id,
    )
    blocking = [c for c in raw if not c["all_day"]]
    if not blocking:
        return None, None

    out = []
    for c in blocking:
        same_day = c["start_dt"].date() == c["end_dt"].date()
        start_s = c["start_dt"].strftime("%-I:%M%p").lower()
        end_s = (
            c["end_dt"].strftime("%-I:%M%p").lower()
            if same_day
            else c["end_dt"].strftime("%a %-I:%M%p").lower()
        )
        out.append({
            "title": c["title"],
            "calendar_name": c["calendar_name"],
            "time_range": f"{start_s}–{end_s}",
            "overlap": c["overlap"],
        })
    return "conflict", out


def _execute_create(db, user, event):
    service = calendar_ops.get_service(user)
    calendar_ops.insert_event(
        service,
        calendar_id=event["calendar_id"],
        title=event["title"],
        date=event["date"],
        time=event["time"],
        duration_minutes=event.get("duration_minutes"),
        location=event.get("location"),
        reminder_minutes=event.get("reminder_minutes"),
    )
    state.clear_pending(db, user)
    return formatted.create_success(event)


def _update(db, user, target_query, changes, message=""):
    if not target_query:
        return formatted.clarify("Which event?")
    if not changes:
        return formatted.clarify("What would you like to change?")

    cal_hint = changes.calendar
    if not (cal_hint and cal_hint.strip()):
        cal_hint = _calendar_from_text(message)

    if not changes.model_dump(exclude_none=True) and not cal_hint:
        return formatted.clarify("What would you like to change?")

    service = calendar_ops.get_service(user)
    match = calendar_ops.find_upcoming_event(user, service, target_query)
    if not match:
        return formatted.not_found(target_query)

    original = _event_to_dict(match)

    new_cal_id = original["calendar_id"]
    new_cal_name = original["calendar_name"]
    if cal_hint and cal_hint.strip():
        resolved = calendar_ops.resolve_calendar(user, service, cal_hint)
        if resolved is None:
            return formatted.clarify(
                f"Couldn't find a calendar matching '{cal_hint}'. "
                f"Text 'what calendars do I have' to see your options."
            )
        new_cal_id, new_cal_name = resolved

    new_event = {**original}
    for k, v in changes.model_dump(exclude_none=True).items():
        if k == "calendar":
            continue
        new_event[k] = v
    new_event["calendar_id"] = new_cal_id
    new_event["calendar_name"] = new_cal_name

    if not changes.model_dump(exclude_none=True).get("reminder_minutes") and getattr(changes, "reminder_at", None):
        new_event["reminder_minutes"] = _resolve_reminder(changes.reminder_at, new_event["date"], new_event["time"])

    warning, conflicts = _detect_warning(user, service, new_event, exclude_event_id=match["id"])
    payload = {
        "kind": "update",
        "event_id": match["id"],
        "event": new_event,
        "original": original,
        "warning": warning,
    }
    if conflicts:
        payload["conflicts"] = conflicts
    state.set_pending(db, user, payload)

    diff_lines = _build_update_diff(original, new_event)
    return formatted.update_confirmation(original["title"], diff_lines, conflicts)


def _build_update_diff(original, new_event):
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
    if (original.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES) != (new_event.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES):
        lines.append(f"Reminder: {original.get('reminder_minutes') or DEFAULT_REMINDER_MINUTES} → {new_event.get('reminder_minutes') or DEFAULT_REMINDER_MINUTES} min before")
    if original["calendar_id"] != new_event["calendar_id"]:
        lines.append(f"Calendar: {original['calendar_name']} → {new_event['calendar_name']}")
    return lines or ["(no changes)"]


def _execute_update(db, user, pending):
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
            reminder_minutes=new_event.get("reminder_minutes"),
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
        if (new_event.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES) != (original.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES):
            fields_to_patch["reminder_minutes"] = new_event.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES

        if fields_to_patch:
            calendar_ops.patch_event(service, new_event["calendar_id"], pending["event_id"], fields_to_patch)

    state.clear_pending(db, user)
    return formatted.update_success(new_event["title"])


def _delete(db, user, target_query):
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


def _execute_delete(db, user, pending):
    service = calendar_ops.get_service(user)
    calendar_ops.delete_event(service, pending["calendar_id"], pending["event_id"])
    state.clear_pending(db, user)
    return formatted.delete_success(pending["title"])


def _list(user, list_date):
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


def _detail(user, target_query):
    if not target_query:
        return formatted.clarify("Which event?")

    service = calendar_ops.get_service(user)
    match = calendar_ops.find_upcoming_event(user, service, target_query)
    if not match:
        return formatted.not_found(target_query)
    return formatted.event_detail(match)


def _list_calendars(user):
    service = calendar_ops.get_service(user)
    calendars = calendar_ops.get_user_calendars(user, service)
    return formatted.calendar_names([c["name"] for c in calendars])


def _event_to_dict(gcal_event):
    start = gcal_event["start"].get("dateTime") or gcal_event["start"].get("date")
    end = gcal_event["end"].get("dateTime") or gcal_event["end"].get("date")
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(TZ)
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(TZ)
    overrides = gcal_event.get("reminders", {}).get("overrides", [])
    reminder_minutes = overrides[0]["minutes"] if overrides else None
    return {
        "title": gcal_event.get("summary", "(untitled)"),
        "date": start_dt.strftime("%Y-%m-%d"),
        "time": start_dt.strftime("%H:%M"),
        "duration_minutes": int((end_dt - start_dt).total_seconds() / 60),
        "location": gcal_event.get("location") or "",
        "calendar_id": gcal_event["_calendar_id"],
        "calendar_name": gcal_event["_calendar_name"],
        "reminder_minutes": reminder_minutes,
    }