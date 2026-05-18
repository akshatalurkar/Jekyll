import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import formatted
from . import state
from . import calendar_ops
from .models import CalendarAction

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
DEFAULT_REMINDER_MINUTES = 30
LIST_SCOPE_DAYS = 1
MAX_DISAMBIG = 5

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
        return None, None
    start = datetime.strptime(f"{event_date} {event_time}", "%Y-%m-%d %H:%M")
    rem = datetime.strptime(f"{event_date} {reminder_at}", "%Y-%m-%d %H:%M")
    minutes = int((start - rem).total_seconds() / 60)
    if minutes < 0:
        return 0, "That reminder time is after the event starts — set to the start of the event instead."
    return minutes, None


def _make_match_summary(match):
    start_raw = match["start"].get("dateTime") or match["start"].get("date")
    is_all_day = "dateTime" not in match["start"]
    if is_all_day:
        dt = datetime.strptime(start_raw, "%Y-%m-%d")
        when = dt.strftime("%a %b %-d") + " (all day)"
    else:
        dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(TZ)
        when = dt.strftime("%a %b %-d at %-I:%M%p").lower()
    return {
        "id": match["id"],
        "calendar_id": match.get("_calendar_id"),
        "calendar_name": match.get("_calendar_name", "Default"),
        "title": match.get("summary", "(untitled)"),
        "when": when,
        "recurring_event_id": match.get("recurringEventId"),
        "event_dict": _event_to_dict(match) if not is_all_day else None,
    }


def _event_to_dict(gcal_event):
    start_raw = gcal_event["start"].get("dateTime") or gcal_event["start"].get("date")
    end_raw = gcal_event["end"].get("dateTime") or gcal_event["end"].get("date")
    is_all_day = "dateTime" not in gcal_event["start"]
    if is_all_day:
        start_dt = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=TZ)
        end_dt = datetime.strptime(end_raw, "%Y-%m-%d").replace(tzinfo=TZ)
    else:
        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(TZ)
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(TZ)
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
        "is_all_day": is_all_day,
    }


def _with_pending_reminder(pending, response):
    if not pending:
        return response
    kind = pending.get("kind")
    if kind == "disambiguate":
        return response + "\n\n_(Still waiting for your selection. Reply with a number or *No* to cancel.)_"
    event = pending.get("event") or {}
    title = event.get("title")
    if not title:
        return response
    verb = {"create": "add", "update": "update", "delete": "remove"}.get(kind, "confirm")
    return response + f"\n\n_(Still waiting to {verb} *{title}*. Reply *Yes* or *No*.)_"


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
        return _with_pending_reminder(pending, _refresh(db, user))

    if pending and action.action == "create":
        if action.event and action.event.title:
            old_title = (pending.get("event") or {}).get("title")
            old_kind = pending.get("kind")
            state.clear_pending(db, user)
            result = _create(db, user, action.event, message)
            if old_title and old_kind in ("create", "update", "delete"):
                verb = {"create": "scheduling", "update": "editing", "delete": "removing"}.get(old_kind, "pending action for")
                result = f"_(Setting aside {verb} *{old_title}*.)_\n\n" + result
            elif old_kind == "disambiguate":
                result = "_(Setting aside your previous selection.)_\n\n" + result
            return result
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
        return _with_pending_reminder(pending, _list(user, action.list_date))

    if action.action == "detail":
        return _with_pending_reminder(pending, _detail(db, user, action.target_query))

    if action.action == "list_calendars":
        return _with_pending_reminder(pending, _list_calendars(user))

    return formatted.error()


def resolve_disambiguate(db, user, pending, selection):
    intent = pending.get("intent")
    matches = pending.get("matches", [])
    service = calendar_ops.get_service(user)

    if selection == "all":
        if intent != "delete":
            return "You can only remove all matches at once, not edit or view them all."
        titles_preview = ", ".join(f"*{m['title']}*" for m in matches)
        payload = {
            "kind": "bulk_delete",
            "matches": matches,
        }
        state.set_pending(db, user, payload)
        count = len(matches)
        noun = "event" if count == 1 else "events"
        return f"Remove all {count} {noun} ({titles_preview})?\n\n*Yes* / *No*"

    idx = int(selection) - 1
    m = matches[idx]
    state.clear_pending(db, user)

    if intent == "delete":
        payload = {
            "kind": "delete",
            "event_id": m["id"],
            "calendar_id": m["calendar_id"],
            "title": m["title"],
            "when": m["when"],
        }
        state.set_pending(db, user, payload)
        result = formatted.delete_confirmation(m["title"], m["when"])
        if m.get("recurring_event_id"):
            result = formatted.recurring_delete_warning(m["title"]) + "\n\n" + result
        return result

    if intent == "update":
        event_dict = m.get("event_dict")
        if not event_dict:
            return "I can't edit all-day events. Try a timed event instead."
        # Re-fetch to get current state — stored snapshot may be stale
        try:
            fresh = service.events().get(
                calendarId=m["calendar_id"],
                eventId=m["id"],
            ).execute()
            fresh["_calendar_id"] = m["calendar_id"]
            fresh["_calendar_name"] = m["calendar_name"]
            if "dateTime" in fresh.get("start", {}):
                event_dict = _event_to_dict(fresh)
        except Exception:
            pass  # Fall back to stored snapshot on fetch failure

        changes_dump = pending.get("changes_dump", {})
        cal_hint = pending.get("cal_hint")

        new_cal_id = event_dict["calendar_id"]
        new_cal_name = event_dict["calendar_name"]
        if cal_hint:
            resolved = calendar_ops.resolve_calendar(user, service, cal_hint)
            if resolved is None:
                return formatted.clarify(
                    f"Couldn't find a calendar matching '{cal_hint}'. "
                    "Reply *refresh* to sync, or *what calendars do I have* to see your options."
                )
            new_cal_id, new_cal_name = resolved

        new_event = {**event_dict}
        for k, v in changes_dump.items():
            if k in ("calendar", "reminder_at"):
                continue
            new_event[k] = v
        new_event["calendar_id"] = new_cal_id
        new_event["calendar_name"] = new_cal_name

        reminder_at = changes_dump.get("reminder_at")
        if not changes_dump.get("reminder_minutes") and reminder_at:
            mins, rem_warn = _resolve_reminder(reminder_at, new_event["date"], new_event["time"])
            new_event["reminder_minutes"] = mins

        warning, conflicts = _detect_warning(user, service, new_event, exclude_event_id=m["id"])
        payload = {
            "kind": "update",
            "event_id": m["id"],
            "event": new_event,
            "original": event_dict,
            "warning": warning,
        }
        if conflicts:
            payload["conflicts"] = conflicts
        state.set_pending(db, user, payload)

        diff_lines = _build_update_diff(event_dict, new_event)
        result = formatted.update_confirmation(event_dict["title"], diff_lines, conflicts)
        if m.get("recurring_event_id"):
            result = formatted.recurring_update_warning(m["title"]) + "\n\n" + result
        return result

    if intent == "detail":
        try:
            event = service.events().get(
                calendarId=m["calendar_id"],
                eventId=m["id"],
            ).execute()
            event["_calendar_id"] = m["calendar_id"]
            event["_calendar_name"] = m["calendar_name"]
            return formatted.event_detail(event)
        except Exception:
            return formatted.not_found(m["title"])

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

    kind = pending.get("kind")
    try:
        if kind == "create":
            return _execute_create(db, user, pending["event"])
        if kind == "update":
            return _execute_update(db, user, pending)
        if kind == "delete":
            return _execute_delete(db, user, pending)
        if kind == "bulk_delete":
            return _execute_bulk_delete(db, user, pending)
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
    if kind in ("delete", "bulk_delete"):
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
            "Reply *refresh* to sync, or *what calendars do I have* to see your options."
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

    reminder_warning = None
    if not event.get("reminder_minutes") and event_fields.reminder_at:
        mins, reminder_warning = _resolve_reminder(event_fields.reminder_at, event["date"], event["time"])
        event["reminder_minutes"] = mins

    warning, conflicts = _detect_warning(user, service, event)
    payload = {"kind": "create", "event": event, "warning": warning}
    if conflicts:
        payload["conflicts"] = conflicts

    state.set_pending(db, user, payload)
    result = formatted.create_confirmation(event, warning, conflicts)
    if reminder_warning:
        result = f"⚠️ {reminder_warning}\n\n" + result
    return result


def _correction(db, user, pending, event_fields, message=""):
    if not event_fields:
        return formatted.clarify("What would you like to change?")

    kind = pending.get("kind")
    if kind == "delete":
        return formatted.clarify("Reply *Yes* to confirm the deletion or *No* to cancel.")

    current = pending.get("event", {})
    merged = {**current}
    for k, v in event_fields.model_dump(exclude_none=True).items():
        if k in ("calendar", "reminder_at"):
            continue
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
                "Reply *refresh* to sync, or *what calendars do I have* to see your options."
            )
        merged["calendar_id"], merged["calendar_name"] = resolved
    merged.pop("calendar", None)

    if merged == current and not event_fields.reminder_at:
        return formatted.clarify(
            "I didn't catch a change. You can adjust the time, date, "
            "calendar, location, duration, or reminder."
        )

    if kind == "create":
        if not merged.get("date") or not merged.get("time"):
            state.set_pending(db, user, {"kind": "create", "event": merged, "warning": None})
            return formatted.clarify("What date and time?")

        reminder_warning = None
        if event_fields.reminder_at:
            mins, reminder_warning = _resolve_reminder(event_fields.reminder_at, merged["date"], merged["time"])
            merged["reminder_minutes"] = mins

        service = calendar_ops.get_service(user)
        warning, conflicts = _detect_warning(user, service, merged)
        payload = {"kind": "create", "event": merged, "warning": warning}
        if conflicts:
            payload["conflicts"] = conflicts
        state.set_pending(db, user, payload)
        result = formatted.create_confirmation(merged, warning, conflicts)
        if reminder_warning:
            result = f"⚠️ {reminder_warning}\n\n" + result
        return result

    if kind == "update":
        reminder_warning = None
        if event_fields.reminder_at and merged.get("date") and merged.get("time"):
            mins, reminder_warning = _resolve_reminder(event_fields.reminder_at, merged["date"], merged["time"])
            merged["reminder_minutes"] = mins

        service = calendar_ops.get_service(user)
        warning, conflicts = _detect_warning(user, service, merged, exclude_event_id=pending["event_id"])
        payload = {
            "kind": "update",
            "event_id": pending["event_id"],
            "event": merged,
            "original": pending["original"],
            "warning": warning,
        }
        if conflicts:
            payload["conflicts"] = conflicts
        state.set_pending(db, user, payload)
        diff_lines = _build_update_diff(pending["original"], merged)
        result = formatted.update_confirmation(pending["original"]["title"], diff_lines, conflicts)
        if reminder_warning:
            result = f"⚠️ {reminder_warning}\n\n" + result
        return result

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
    matches = calendar_ops.find_matching_events(user, service, target_query)
    if not matches:
        return formatted.not_found(target_query)

    if len(matches) > 1:
        truncated = max(0, len(matches) - MAX_DISAMBIG)
        summaries = [_make_match_summary(m) for m in matches[:MAX_DISAMBIG]]
        payload = {
            "kind": "disambiguate",
            "intent": "update",
            "matches": summaries,
            "changes_dump": changes.model_dump(exclude_none=True),
            "cal_hint": cal_hint,
            "message": message,
        }
        state.set_pending(db, user, payload)
        return formatted.disambiguate(summaries, "update", truncated)

    match = matches[0]

    if "dateTime" not in match.get("start", {}):
        return formatted.clarify("I can't edit all-day events. Try a timed event instead.")

    original = _event_to_dict(match)

    new_cal_id = original["calendar_id"]
    new_cal_name = original["calendar_name"]
    if cal_hint and cal_hint.strip():
        resolved = calendar_ops.resolve_calendar(user, service, cal_hint)
        if resolved is None:
            return formatted.clarify(
                f"Couldn't find a calendar matching '{cal_hint}'. "
                "Reply *refresh* to sync, or *what calendars do I have* to see your options."
            )
        new_cal_id, new_cal_name = resolved

    new_event = {**original}
    for k, v in changes.model_dump(exclude_none=True).items():
        if k in ("calendar", "reminder_at"):
            continue
        new_event[k] = v
    new_event["calendar_id"] = new_cal_id
    new_event["calendar_name"] = new_cal_name

    reminder_warning = None
    if not changes.model_dump(exclude_none=True).get("reminder_minutes") and getattr(changes, "reminder_at", None):
        mins, reminder_warning = _resolve_reminder(changes.reminder_at, new_event["date"], new_event["time"])
        new_event["reminder_minutes"] = mins

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
    result = formatted.update_confirmation(original["title"], diff_lines, conflicts)
    if match.get("recurringEventId"):
        result = formatted.recurring_update_warning(original["title"]) + "\n\n" + result
    if reminder_warning:
        result = f"⚠️ {reminder_warning}\n\n" + result
    return result


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
        calendar_ops.delete_event(service, original["calendar_id"], pending["event_id"])
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
    matches = calendar_ops.find_matching_events(user, service, target_query)
    if not matches:
        return formatted.not_found(target_query)

    if len(matches) > 1:
        truncated = max(0, len(matches) - MAX_DISAMBIG)
        summaries = [_make_match_summary(m) for m in matches[:MAX_DISAMBIG]]
        payload = {
            "kind": "disambiguate",
            "intent": "delete",
            "matches": summaries,
        }
        state.set_pending(db, user, payload)
        return formatted.disambiguate(summaries, "delete", truncated)

    match = matches[0]
    start_raw = match["start"].get("dateTime") or match["start"].get("date")
    if "dateTime" in match["start"]:
        dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(TZ)
        when = dt.strftime("%a %b %-d at %-I:%M%p").lower()
    else:
        dt = datetime.strptime(start_raw, "%Y-%m-%d")
        when = dt.strftime("%a %b %-d") + " (all day)"

    payload = {
        "kind": "delete",
        "event_id": match["id"],
        "calendar_id": match["_calendar_id"],
        "title": match.get("summary", "(untitled)"),
        "when": when,
    }
    state.set_pending(db, user, payload)
    result = formatted.delete_confirmation(payload["title"], when)
    if match.get("recurringEventId"):
        result = formatted.recurring_delete_warning(payload["title"]) + "\n\n" + result
    return result


def _execute_bulk_delete(db, user, pending):
    service = calendar_ops.get_service(user)
    titles = []
    failed = []
    for m in pending.get("matches", []):
        try:
            calendar_ops.delete_event(service, m["calendar_id"], m["id"])
            titles.append(m["title"])
        except Exception:
            failed.append(m["title"])
    state.clear_pending(db, user)
    if not titles and failed:
        return "Couldn't remove any of those events. Try again."
    result = "✓ Removed: " + ", ".join(f"*{t}*" for t in titles)
    if failed:
        result += "\nCouldn't remove: " + ", ".join(f"*{t}*" for t in failed)
    return result


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


def _detail(db, user, target_query):
    if not target_query:
        return formatted.clarify("Which event?")

    service = calendar_ops.get_service(user)
    matches = calendar_ops.find_matching_events(user, service, target_query)
    if not matches:
        return formatted.not_found(target_query)

    if len(matches) > 1:
        truncated = max(0, len(matches) - MAX_DISAMBIG)
        summaries = [_make_match_summary(m) for m in matches[:MAX_DISAMBIG]]
        state.set_pending(db, user, {
            "kind": "disambiguate",
            "intent": "detail",
            "matches": summaries,
        })
        return formatted.disambiguate(summaries, "detail", truncated)

    match = matches[0]
    match["_calendar_id"] = match.get("_calendar_id")
    match["_calendar_name"] = match.get("_calendar_name", "Default")
    return formatted.event_detail(match)


def _list_calendars(user):
    service = calendar_ops.get_service(user)
    calendars = calendar_ops.get_user_calendars(user, service)
    return formatted.calendar_names([c["name"] for c in calendars])
