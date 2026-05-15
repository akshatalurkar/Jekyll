"""
Every string the user sees, built deterministically.
Change the voice here; nowhere else.

WhatsApp markdown: *bold*, _italic_, ~strike~, ```code```
"""

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")


# ── Date / time formatting ──────────────────────────────────

def fmt_date(date_iso: str) -> str:
    """YYYY-MM-DD → 'today', 'tomorrow', 'yesterday', or 'Fri May 17'."""
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


def fmt_time(time_24h: str) -> str:
    """HH:MM → '3:00pm'."""
    h, m = map(int, time_24h.split(":"))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


def fmt_event_time_from_iso(iso_str: str) -> str:
    """For Google Calendar event start strings."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%-I:%M%p").lower()


# ── Create / correction confirmations ───────────────────────

_WARNING_PREFIX = {
    "past": "⚠️ This is in the past.",
    "now":  "⚠️ This is happening right now.",
    "conflict": None,  # built dynamically with conflict event info
}


def create_confirmation(event: dict, warning: str | None, conflict: dict | None = None) -> str:
    lines = []
    if warning == "conflict" and conflict:
        lines.append(
            f"⚠️ Conflicts with *{conflict['title']}* at {conflict['time']}"
        )
    elif warning in _WARNING_PREFIX:
        lines.append(_WARNING_PREFIX[warning])

    lines.append("Here's what I'll add:" if not warning else "")
    lines.append("")
    lines.append(f"*{event['title']}*")
    lines.append(f"{fmt_date(event['date'])} at {fmt_time(event['time'])}")

    dur = event.get("duration_minutes")
    if dur:
        lines.append(f"{dur} min")
    else:
        lines.append("60 min (default)")

    if event.get("location"):
        lines.append(event["location"])
    else:
        lines.append("No location")

    lines.append(f"Calendar: {event.get('calendar_name', 'Default')}")
    lines.append("")
    lines.append("Reply *Yes* to confirm, *No* to cancel, or send a correction.")
    return "\n".join(l for l in lines if l is not None)


def create_success(event: dict) -> str:
    location = f" at {event['location']}" if event.get("location") else ""
    return f"✓ Added — *{event['title']}*{location} on {fmt_date(event['date'])} at {fmt_time(event['time'])}"


# ── Update confirmations ────────────────────────────────────

def update_confirmation(original_title: str, diff_lines: list[str]) -> str:
    return (
        f"Here's what I'll change for *{original_title}*:\n\n"
        + "\n".join(diff_lines)
        + "\n\nReply *Yes* to confirm, *No* to cancel, or send a correction."
    )


def update_success(title: str) -> str:
    return f"✓ Updated — *{title}*"


# ── Delete confirmation ─────────────────────────────────────

def delete_confirmation(title: str, when: str) -> str:
    return (
        f"Remove *{title}* on {when}?\n\n"
        f"Reply *Yes* to confirm or *No* to cancel."
    )


def delete_success(title: str) -> str:
    return f"✓ Removed — *{title}*"


# ── List ────────────────────────────────────────────────────

def list_grouped(label: str, date_label: str | None, events: list[dict]) -> str:
    if not events:
        when = f" {label}" if label else ""
        return f"Nothing scheduled{when}."

    # Group by calendar name
    groups: dict[str, list[dict]] = {}
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


def list_out_of_scope() -> str:
    return "I can only show today, tomorrow, or yesterday."


# ── Detail ──────────────────────────────────────────────────

def event_detail(event: dict) -> str:
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
    lines.append(f"Calendar: {event.get('_calendar_name', 'Default')}")
    return "\n".join(lines)


# ── Calendar list ───────────────────────────────────────────

def calendar_names(names: list[str]) -> str:
    if not names:
        return "No calendars found."
    return "Your calendars:\n" + "\n".join(f"• {n}" for n in names)


# ── Cancellation / confirmation outcomes ────────────────────

def cancelled_create() -> str:
    return "OK, not adding it."


def cancelled_update() -> str:
    return "OK, leaving it as-is."


def cancelled_delete() -> str:
    return "OK, keeping it on your calendar."


def nothing_pending() -> str:
    return "Nothing pending. What would you like to do?"


def pending_timed_out() -> str:
    return "That timed out. What would you like to do?"


# ── Fallbacks ───────────────────────────────────────────────

def clarify(question: str | None) -> str:
    return question or "Need more info."


def rejected() -> str:
    return "Calendar only."


def not_found(keyword: str) -> str:
    return f"Couldn't find '{keyword}'."


def error() -> str:
    return "Something went wrong. Try again."