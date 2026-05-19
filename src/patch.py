import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import formatted
from . import state
from . import calendar_ops
from .models import CalendarAction
from .core import log_event

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DURATION_MINUTES = 60
DEFAULT_REMINDER_MINUTES = 30
LIST_SCOPE_DAYS = 1
MAX_DISAMBIG = 5

_CAL_TO_X = re.compile(r"calendars?\s+to\s+(.+)", re.I)
_TO_X_CAL = re.compile(r"\bto\s+(?:the\s+|my\s+)?(.+?)\s+calendars?\b", re.I)

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
# Words that mark the *destination* of a change. Anything appearing AFTER one
# of these is treated as the new value, not as identifying context.
_DESTINATION_MARKERS = re.compile(
    r"\b(?:to|into|on\s+to|onto|→|->|over\s+to|push\s+to|move\s+to|"
    r"reschedule\s+to|change(?:d)?\s+to|change\s+it\s+to|set\s+to|make\s+it)\b",
    re.I,
)
_DATE_WORD_RE = re.compile(
    r"\b(today|tomorrow|tmrw|tmr|yesterday|tonight|"
    r"this\s+(?:morning|afternoon|evening|week|weekend)|"
    r"next\s+(?:week|weekend|"
    + "|".join(_WEEKDAYS.keys()) + r")|"
    + r"|".join(_WEEKDAYS.keys()) + r"|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|"
    r"\d{4}-\d{2}-\d{2}|"
    r"(?:" + "|".join(_MONTHS.keys()) + r")\s+\d{1,2}(?:st|nd|rd|th)?"
    r")\b",
    re.I,
)


def _resolve_date_hint(token, now):
    """Resolve a date-hint token to a YYYY-MM-DD string, or None."""
    t = token.lower().strip()
    today = now.date()

    if t in ("today", "tonight") or t.startswith("this morning") or t.startswith("this afternoon") or t.startswith("this evening"):
        return today.strftime("%Y-%m-%d")
    if t in ("tomorrow", "tmrw", "tmr"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if t == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # "next monday", "next friday" — interpret as the weekday in the upcoming
    # week (always 7+ days out).
    if t.startswith("next "):
        rest = t[5:].strip()
        if rest in _WEEKDAYS:
            target = _WEEKDAYS[rest]
            days_ahead = (target - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        # "next week"/"next weekend" — too vague to filter on, skip
        return None

    # bare weekday — the upcoming occurrence (today counts only if not past)
    if t in _WEEKDAYS:
        target = _WEEKDAYS[t]
        days_ahead = (target - today.weekday()) % 7
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # ISO YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return t

    # M/D or M/D/YY(YY)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y:
            y = int(y)
            if y < 100:
                y += 2000
        else:
            y = today.year
            # If the parsed date is well in the past, assume next year
            try:
                candidate = datetime(y, mo, d).date()
                if (today - candidate).days > 180:
                    y += 1
            except ValueError:
                return None
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # "may 20", "december 3"
    m = re.fullmatch(r"(" + "|".join(_MONTHS.keys()) + r")\s+(\d{1,2})(?:st|nd|rd|th)?", t)
    if m:
        mo = _MONTHS[m.group(1)]
        d = int(m.group(2))
        y = today.year
        try:
            candidate = datetime(y, mo, d).date()
            if (today - candidate).days > 180:
                y += 1
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def _extract_identifying_dates(message, intent):
    """Pull date hints from `message` that identify (not change) an event.

    For `update`, anything after a destination marker ("to", "into", …) is
    the NEW value and is excluded. For `delete`, all date hints are
    identifying. Returns a set of YYYY-MM-DD strings.
    """
    if not message:
        return set()

    if intent == "update":
        # Cut the message at the first destination marker.
        m = _DESTINATION_MARKERS.search(message)
        scope = message[: m.start()] if m else message
    else:
        scope = message

    now = datetime.now(TZ)
    found = set()
    for raw in _DATE_WORD_RE.findall(scope):
        token = raw if isinstance(raw, str) else raw[0]
        resolved = _resolve_date_hint(token, now)
        if resolved:
            found.add(resolved)
    return found


def _narrow_matches(matches, message, intent):
    """Narrow a list of disambiguation candidates using date context in
    `message`. Falls back to the full list when no narrowing applies or when
    narrowing would eliminate every candidate.
    """
    if not matches or len(matches) == 1:
        return matches

    target_dates = _extract_identifying_dates(message, intent)
    if not target_dates:
        return matches

    narrowed = []
    for ev in matches:
        start = ev.get("start", {})
        start_raw = start.get("dateTime") or start.get("date")
        if not start_raw:
            continue
        if "dateTime" in start:
            try:
                ev_date = datetime.fromisoformat(
                    start_raw.replace("Z", "+00:00")
                ).astimezone(TZ).strftime("%Y-%m-%d")
            except ValueError:
                continue
        else:
            ev_date = start_raw[:10]
        if ev_date in target_dates:
            narrowed.append(ev)

    return narrowed if narrowed else matches


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
        rem_prev = rem - timedelta(days=1)
        prev_minutes = int((start - rem_prev).total_seconds() / 60)
        if prev_minutes > 0:
            return prev_minutes, None
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


def _prepend_setting_aside(old_pending, new_pending, response):
    if not old_pending:
        return response
    if new_pending == old_pending:
        return response
    kind = old_pending.get("kind")
    if kind in ("create", "update", "delete", "bulk_delete"):
        # delete/bulk_delete store `title` at top-level; create/update nest it
        # under `event`. Try both so the "Setting aside …" prefix renders for
        # all kinds.
        title = (old_pending.get("event") or {}).get("title") or old_pending.get("title")
        if not title and kind == "bulk_delete":
            matches = old_pending.get("matches") or []
            if matches:
                title = matches[0].get("title")
                if len(matches) > 1:
                    title = f"{title} + {len(matches) - 1} more"
        if not title:
            return response
        verb = {
            "create": "scheduling",
            "update": "editing",
            "delete": "removing",
            "bulk_delete": "removing",
        }.get(kind)
        return f"_(Setting aside {verb} *{title}*.)_\n\n" + response
    if kind == "disambiguate":
        return "_(Setting aside your previous selection.)_\n\n" + response
    return response


def _with_pending_reminder(pending, response):
    if not pending:
        return response
    kind = pending.get("kind")
    if kind == "disambiguate":
        return response + "\n\n_(Still waiting for your selection. Reply with a number or *No* to cancel.)_"
    event = pending.get("event") or {}
    title = event.get("title") or pending.get("title")
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
        refresh_result = _refresh(db, user)
        return _with_pending_reminder(state.get_pending(user), refresh_result)

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
        result = _update(db, user, action.target_query, action.event, message)
        return _prepend_setting_aside(pending, state.get_pending(user), result)

    if action.action == "delete":
        result = _delete(db, user, action.target_query, message)
        return _prepend_setting_aside(pending, state.get_pending(user), result)

    if action.action == "list":
        return _with_pending_reminder(pending, _list(user, action.list_date))

    if action.action == "detail":
        result = _detail(db, user, action.target_query)
        new_pending = state.get_pending(user)
        if new_pending != pending and pending and pending.get("kind") in ("create", "update", "disambiguate"):
            return _prepend_setting_aside(pending, new_pending, result)
        return _with_pending_reminder(pending, result)

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
    if idx < 0 or idx >= len(matches):
        return formatted.error()
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
    result = "Refreshed."
    pending = state.get_pending(user)
    if pending and pending.get("kind") == "disambiguate":
        state.clear_pending(db, user)
        result += "\n\nYour pending selection was cleared — please try your search again."
    return result


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
        log_event("confirm_error", user_id=user.id, kind=kind, error_type=type(e).__name__)
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

    state.clear_pending(db, user)
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
    log_event("event_created", user_id=user.id, calendar_id=event.get("calendar_id"))
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
        matches = _narrow_matches(matches, message, "update")

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

    diff_lines = _build_update_diff(original, new_event)
    if diff_lines == ["(no changes)"]:
        return formatted.clarify("I didn't catch a change — what would you like to update?")

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
    # Compare reminders by raw value (None = no reminder set). Coalescing both
    # sides to DEFAULT_REMINDER_MINUTES would hide the case where a user adds
    # a 30-min reminder to an event that had no reminder (None → 30 would look
    # equal after coalescing).
    orig_rem = original.get("reminder_minutes")
    new_rem = new_event.get("reminder_minutes")
    if orig_rem != new_rem:
        old_disp = f"{orig_rem} min before" if orig_rem is not None else "(none)"
        new_disp = f"{new_rem} min before" if new_rem is not None else "(none)"
        lines.append(f"Reminder: {old_disp} → {new_disp}")
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
        try:
            calendar_ops.delete_event(service, original["calendar_id"], pending["event_id"])
        except Exception as e:
            log_event("cross_cal_move_delete_failed", user_id=user.id, error_type=type(e).__name__)
            state.clear_pending(db, user)
            return (
                f"Event moved to {new_event['calendar_name']} but the original copy "
                f"couldn't be removed — you may need to delete it manually."
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
        # Mirror _build_update_diff: compare raw values so adding a reminder
        # equal to DEFAULT_REMINDER_MINUTES to an event with no reminder still
        # fires the patch (None → 30 must be detected as a change).
        if new_event.get("reminder_minutes") != original.get("reminder_minutes"):
            fields_to_patch["reminder_minutes"] = (
                new_event.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES
            )

        if fields_to_patch:
            calendar_ops.patch_event(service, new_event["calendar_id"], pending["event_id"], fields_to_patch)

    state.clear_pending(db, user)
    log_event("event_updated", user_id=user.id, calendar_id=new_event.get("calendar_id"))
    return formatted.update_success(new_event["title"])


def _delete(db, user, target_query, message=""):
    if not target_query:
        return formatted.clarify("Which event should I remove?")

    service = calendar_ops.get_service(user)
    matches = calendar_ops.find_matching_events(user, service, target_query)
    if not matches:
        return formatted.not_found(target_query)

    if len(matches) > 1:
        matches = _narrow_matches(matches, message, "delete")

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
    log_event("events_bulk_deleted", user_id=user.id, count=len(titles), failed=len(failed))
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
    log_event("event_deleted", user_id=user.id, calendar_id=pending.get("calendar_id"))
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
    return formatted.event_detail(match)


def _list_calendars(user):
    service = calendar_ops.get_service(user)
    calendars = calendar_ops.get_user_calendars(user, service)
    return formatted.calendar_names([c["name"] for c in calendars])
