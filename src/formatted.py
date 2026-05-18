from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_REMINDER_MINUTES = 30


def fmt_date(date_iso):
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    today = datetime.now(TZ).date()
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return d.strftime("%a %b %-d")


def fmt_time(time_24h):
    h, m = map(int, time_24h.split(":"))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


def fmt_event_time_from_iso(iso_str):
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%-I:%M%p").lower()


def _fmt_datetime(date_iso, time_24h):
    date_str = fmt_date(date_iso)
    time_str = fmt_time(time_24h)
    combined = f"{date_str} at {time_str}"
    return combined[0].upper() + combined[1:]


def create_confirmation(event, warning, conflicts=None):
    lines = []

    if warning == "conflict" and conflicts:
        if len(conflicts) == 1:
            c = conflicts[0]
            lines.append(f"⚠️ Conflicts with *{c['title']}* ({c['calendar_name']}) — {c['time_range']}")
        else:
            lines.append(f"⚠️ Overlaps {len(conflicts)} events:")
            for c in conflicts:
                lines.append(f"  • *{c['title']}* ({c['calendar_name']}) — {c['time_range']}")
        lines.append("")
    elif warning == "past":
        lines.append("⚠️ This is in the past.")
        lines.append("")
    elif warning == "now":
        lines.append("⚠️ This is happening right now.")
        lines.append("")

    dur = event.get("duration_minutes") or 60
    rem = event.get("reminder_minutes") or DEFAULT_REMINDER_MINUTES
    cal = event.get("calendar_name") or "Calendar Not Set"
    loc = event.get("location") or "Location Not Set"

    lines.append(f"*{event['title']}*")
    lines.append(_fmt_datetime(event['date'], event['time']))
    lines.append(f"{dur} min")
    lines.append(loc)
    lines.append(f"Calendar: {cal}")
    lines.append(f"Reminder: {rem} min before")
    lines.append("")
    lines.append("Reply *Yes* to confirm, *No* to cancel, or send a correction")
    return "\n".join(lines)


def create_success(event):
    loc = f" at {event['location']}" if event.get("location") else ""
    return f"✓ *{event['title']}*{loc} — {_fmt_datetime(event['date'], event['time'])}"


def update_confirmation(original_title, diff_lines, conflicts=None):
    lines = []

    if conflicts:
        if len(conflicts) == 1:
            c = conflicts[0]
            lines.append(f"⚠️ Conflicts with *{c['title']}* ({c['calendar_name']}) — {c['time_range']}")
        else:
            lines.append(f"⚠️ Overlaps {len(conflicts)} events:")
            for c in conflicts:
                lines.append(f"  • *{c['title']}* ({c['calendar_name']}) — {c['time_range']}")
        lines.append("")

    lines.append(f"*{original_title}*")
    lines.extend(diff_lines)
    lines.append("")
    lines.append("*Yes* / *No* / or edit")
    return "\n".join(lines)


def update_success(title):
    return f"✓ *{title}* updated."


def delete_confirmation(title, when):
    return f"Remove *{title}* on {when}?\n\n*Yes* / *No*"


def delete_success(title):
    return f"✓ *{title}* removed."


def list_grouped(label, date_label, events):
    if not events:
        when = f" {label}" if label else ""
        return f"Nothing scheduled{when}."

    groups = {}
    for e in events:
        cal = e.get("_calendar_name", "Default")
        groups.setdefault(cal, []).append(e)

    header = f"*{label.capitalize()}*"
    if date_label:
        header += f" ({date_label})"

    out = [header]
    for cal_name in sorted(groups.keys()):
        out.append("")
        out.append(f"_{cal_name}_")
        for e in groups[cal_name]:
            start = e["start"].get("dateTime") or e["start"].get("date")
            time_str = fmt_event_time_from_iso(start) if "dateTime" in e["start"] else "all day"
            out.append(f"• {time_str}  {e.get('summary', '(untitled)')}")
    return "\n".join(out)


def list_out_of_scope():
    return "I can only show today, tomorrow, or yesterday."


def event_detail(event):
    start_str = event["start"].get("dateTime") or event["start"].get("date")
    end_str = event["end"].get("dateTime") or event["end"].get("date")
    all_day = "dateTime" not in event["start"]

    if all_day:
        date_label = datetime.strptime(start_str, "%Y-%m-%d").strftime("%A, %b %-d")
        time_label = "All day"
        duration_label = None
    else:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(TZ)
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(TZ)
        date_label = start_dt.strftime("%A, %b %-d")
        time_label = f"{start_dt.strftime('%-I:%M%p').lower()} – {end_dt.strftime('%-I:%M%p').lower()}"
        mins = int((end_dt - start_dt).total_seconds() / 60)
        duration_label = f"{mins} min"

    lines = [f"*{event.get('summary', '(untitled)')}*", date_label, time_label]
    if duration_label:
        lines.append(duration_label)
    if event.get("location"):
        lines.append(event["location"])

    overrides = event.get("reminders", {}).get("overrides", [])
    if overrides:
        lines.append(f"Reminder: {overrides[0]['minutes']} min before")

    lines.append(f"_{event.get('_calendar_name', 'Default')}_")
    return "\n".join(lines)


def calendar_names(names):
    if not names:
        return "No calendars found."
    return "Your calendars:\n" + "\n".join(f"• {n}" for n in names)


def cancelled_create():
    return "Cancelled."


def cancelled_update():
    return "No changes made."


def cancelled_delete():
    return "Kept on your calendar."


def nothing_pending():
    return "Nothing pending. What would you like to schedule?"


def pending_timed_out():
    return "That timed out — what would you like to do?"


def clarify(question):
    return question or "Need more info."


def rejected():
    return "I only handle calendar stuff. What would you like to schedule?"


def not_found(keyword):
    return f"Couldn't find '{keyword}'."


def error():
    return "Something went wrong. Try again."


def disambiguate(matches, intent):
    verb = {"delete": "remove", "update": "edit", "detail": "view details for"}.get(intent, "select")
    lines = [f"I found a few matches. Which would you like to {verb}?", ""]
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}. *{m['title']}* — {m['when']} ({m['calendar_name']})")
    lines.append("")
    if intent == "delete":
        lines.append("Reply with a number, *All* to remove all, or *No* to cancel.")
    else:
        lines.append("Reply with a number or *No* to cancel.")
    return "\n".join(lines)


def recurring_delete_warning(title):
    return f"⚠️ *{title}* is part of a recurring series. This will only remove this one occurrence."


def recurring_update_warning(title):
    return f"⚠️ *{title}* is part of a recurring series. This will only update this one occurrence."