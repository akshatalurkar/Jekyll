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


_WARNING_PREFIX = {
    "past": "⚠️ This is in the past.",
    "now":  "⚠️ This is happening right now.",
    "conflict": None,
}


def _reminder_line(event):
    rem = event.get("reminder_minutes")
    return f"Reminder {rem} min before" if rem else "Reminder 30 min before (default)"


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
    elif warning in _WARNING_PREFIX:
        lines.append(_WARNING_PREFIX[warning])

    lines.append("Here's what I'll add:" if not warning else "")
    lines.append("")
    lines.append(f"*{event['title']}*")
    lines.append(f"{fmt_date(event['date'])} at {fmt_time(event['time'])}")

    dur = event.get("duration_minutes")
    lines.append(f"{dur} min" if dur else "60 min (default)")

    lines.append(event["location"] if event.get("location") else "No location")
    lines.append(f"Calendar: {event.get('calendar_name', 'Default')}")
    lines.append(_reminder_line(event))
    lines.append("")
    lines.append("Reply *Yes* to confirm, *No* to cancel, or send a correction.")
    return "\n".join(l for l in lines if l is not None)


def create_success(event):
    location = f" at {event['location']}" if event.get("location") else ""
    return f"✓ Added — *{event['title']}*{location} on {fmt_date(event['date'])} at {fmt_time(event['time'])}"


def update_confirmation(original_title, diff_lines, conflicts=None):
    lines = []
    if conflicts:
        if len(conflicts) == 1:
            c = conflicts[0]
            lines.append(f"⚠️ Conflicts with *{c['title']}* ({c['calendar_name']}) — {c['time_range']}\n")
        else:
            lines.append(f"⚠️ Overlaps {len(conflicts)} events:")
            for c in conflicts:
                lines.append(f"  • *{c['title']}* ({c['calendar_name']}) — {c['time_range']}")
            lines.append("")

    lines.append(f"Here's what I'll change for *{original_title}*:\n")
    lines.append("\n".join(diff_lines))
    lines.append("\nReply *Yes* to confirm, *No* to cancel, or send a correction.")
    return "\n".join(lines)


def update_success(title):
    return f"✓ Updated — *{title}*"


def delete_confirmation(title, when):
    return (
        f"Remove *{title}* on {when}?\n\n"
        f"Reply *Yes* to confirm or *No* to cancel."
    )


def delete_success(title):
    return f"✓ Removed — *{title}*"


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
            if "dateTime" in e["start"]:
                time_str = fmt_event_time_from_iso(start)
            else:
                time_str = "all day"
            title = e.get("summary", "(untitled)")
            out.append(f"• {time_str}  {title}")
    return "\n".join(out)


def list_out_of_scope():
    return "I can only show today, tomorrow, or yesterday."


def event_detail(event):
    start_str = event["start"].get("dateTime") or event["start"].get("date")
    end_str = event["end"].get("dateTime") or event["end"].get("date")
    all_day = "dateTime" not in event["start"]

    if all_day:
        date_label = datetime.strptime(start_str, "%Y-%m-%d").strftime("%A, %b %-d")
        time_label = "all day"
        duration_label = ""
    else:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(TZ)
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(TZ)
        date_label = start_dt.strftime("%A, %b %-d")
        time_label = f"{start_dt.strftime('%-I:%M%p').lower()} – {end_dt.strftime('%-I:%M%p').lower()}"
        mins = int((end_dt - start_dt).total_seconds() / 60)
        duration_label = f"{mins} min"

    lines = [
        f"*{event.get('summary', '(untitled)')}*",
        date_label,
        time_label,
    ]
    if duration_label:
        lines.append(duration_label)
    if event.get("location"):
        lines.append(event["location"])

    overrides = event.get("reminders", {}).get("overrides", [])
    if overrides:
        lines.append(f"Reminder {overrides[0]['minutes']} min before")

    lines.append(f"Calendar: {event.get('_calendar_name', 'Default')}")
    return "\n".join(lines)


def calendar_names(names):
    if not names:
        return "No calendars found."
    return "Your calendars:\n" + "\n".join(f"• {n}" for n in names)


def cancelled_create():
    return "OK, not adding it."


def cancelled_update():
    return "OK, leaving it as-is."


def cancelled_delete():
    return "OK, keeping it on your calendar."


def nothing_pending():
    return "Nothing pending. What would you like to do?"


def pending_timed_out():
    return "That timed out. What would you like to do?"


def clarify(question):
    return question or "Need more info."


def rejected():
    return "Calendar only."


def not_found(keyword):
    return f"Couldn't find '{keyword}'."


def error():
    return "Something went wrong. Try again."