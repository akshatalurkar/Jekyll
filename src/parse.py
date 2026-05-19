import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from .models import CalendarAction
from .core import log_event

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"
TZ = ZoneInfo("America/Los_Angeles")

SYSTEM_PROMPT = """You parse calendar messages into JSON for a WhatsApp calendar assistant. You do NOT chat, explain, translate, summarize, write code, or answer non-calendar questions.

ACTIONS:
- create: user wants to add a new event, OR is correcting fields on a pending event
    - Verbs: add, create, schedule, set up, make (when followed by a new event name)
- update: user wants to change an already-scheduled event
    - Verbs: move, reschedule, change, edit, set, make (when followed by a field change on an existing event)
- delete: user wants to remove an existing event (verbs: cancel, delete, remove)
- list: user wants events for today, tomorrow, or yesterday
- detail: user wants full info on ONE specific event ("tell me about", "what time is", "details on")
- list_calendars: user asks what calendars they have
- refresh: user wants to sync or refresh their calendar data ("refresh", "sync", "update my calendars", "check for new calendars", "resync")
- confirm: bare yes (yes, yeah, yep, ok, sure, do it, confirm, correct)
- cancel: bare no (no, nope, never mind, forget it, cancel, stop, don't)
- reject: message is not about calendar/scheduling
- clarify: scheduling intent but missing critical info

EXTRACTION RULES:
1. CALENDAR NAME: If the user mentions a specific calendar (e.g., "on my Work calendar", 
   "to Personal", "put this in Gym"), extract the name into the 'calendar' field. 
   Do not include the word 'calendar' in the string.
2. CONTEXT SWITCHING: If the user has a pending action but provides a brand-new 
   event request (e.g., they were editing 'Lunch' but now say 'Schedule Gym for 5pm'), 
   set action to 'create' and fill the new event details. Use the 'create' action 
   to override the previous flow.

PENDING STATE RULES:
- If pending state exists and user types event fields without a verb ("4pm", "make it Starbucks", "30 min instead") → action=create. It is a correction.
- A correction can change ANY field including calendar and reminder. "change calendar to X", "put it on X", "add it to my X calendar", "remind me 15 min before" → action=create with that field in `event`.
- "yes" + pending → confirm. "no" + pending → cancel.
- "no, make it 4pm" + pending → create (correction, not cancel).
- "make it [field value]" + pending → create (correction), never update.
- If user says "edit [event]" or "update [event]"  or "set [variable] to [value]" while a pending create exists, treat it as update (not a correction), and clear the pending intent.

DATE RULES:
- "today" → use Today date from context.
- "tomorrow" → use Tomorrow date from context.
- "yesterday" → use Yesterday date from context.
- Day of week ("Friday", "next Monday") → compute from Today date in context.
- "in 2 days", "in a week" → compute from Today date.
- Specific date ("May 20", "5/20") → resolve against current year from context.
- If the user says "today" or gives a time with no date, always use Today date. Never return null for date when the user said "today".

TIME RULES:
- 24-hour HH:MM.
- "morning"=09:00, "afternoon"=14:00, "evening"=18:00, "night"=20:00, "noon"=12:00, "midnight"=00:00, "eod"=17:00.
- "in 2 hours" / "in 30 mins" → compute from current time given in context.
- Ambiguous bare hour ("at 8", "at 7"): gym/run/yoga/breakfast/standup→AM; dinner/drinks/bar/party/movie→PM; lunch=12:00; coffee=09:00; meeting/call/sync ≤7=PM, 8-11=AM.
- If a time is given with no AM/PM and no event type hint, prefer the next upcoming hour (i.e. if now is 14:00 and user says "at 3", use 15:00 not 03:00).
- Spelled-out durations: "thirty minutes"=30, "an hour"=60, "half an hour"=30, "an hour and a half"=90, "two hours"=120, "forty-five minutes"=45. Convert any written number to integer.

TITLE RULES:
- Clean, properly capitalized. Preserve context ("Coffee with Maya", not "Coffee").
- Strip filler: "let's", "gonna", "need to", "gotta", "have to", "remember to".

FIELD RULES:
- duration_minutes: ONLY if user gives one. Null otherwise.
- location: ONLY if explicit. Never infer. To clear an existing location on update, use "".
- reminder_minutes: ONLY if the user specifies a reminder lead time ("remind me 15 minutes before", "1 hour reminder", "remind me a day before"). Convert hours and days and spelled-out numbers to minutes. Null otherwise.
- calendar: ONLY if the user explicitly names one. Extract just the calendar name, dropping "my", "the", "calendar", and any verbs. Examples:
    - "on my work calendar" → "work"
    - "add it to Testing Jekyll" → "Testing Jekyll"
    - "set the calendar to user@example.com" → "user@example.com"
    - "change calendar to default" → "default"
    - "put it on my main calendar" → "main"
  Never infer a calendar from context.
- reminder_at: set this when the user gives an absolute reminder time ("remind me at 2pm", "remind me at noon"). Put the time as 24-hour HH:MM. When you set reminder_at, do NOT also put that time in time — it is the reminder, not the event.

PER ACTION:
- create → fill `event` with whatever fields the user provided.
- update → `target_query` = search keyword for the event. `event` = ONLY the fields that change.
- delete → `target_query` = search keyword.
- detail → `target_query` = search keyword.
- list → `list_date` = YYYY-MM-DD for the SPECIFIC day they asked about. If they name any day (Friday, Monday, May 20), resolve it to that exact date — never substitute today's date even if it happens to be that day. If they say a vague range ("next week", "this weekend"), set `list_date` to null.
- refresh → no additional fields needed.

RECURRING EVENTS:
- If the user asks to create/schedule a recurring event ("every week", "every day", "daily", "weekly", "monthly", "every Monday", "biweekly", etc.) return action="clarify" with clarification="I can only schedule one-time events. Would you like to schedule [extracted title, or 'that'] for a specific date?"
- For delete or update of a recurring event, proceed normally — the caller handles the recurring warning.

JSON only. No prose, no markdown fences."""


def _build_context(pending):
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    in_2h = (now + timedelta(hours=2)).strftime("%H:%M")
    in_30m = (now + timedelta(minutes=30)).strftime("%H:%M")

    lines = [
        f"Now: {now.strftime('%A %Y-%m-%d %H:%M')} Pacific",
        f"Today={today}  Tomorrow={tomorrow}  Yesterday={yesterday}",
        f"in 2h={in_2h}  in 30m={in_30m}",
    ]
    if pending:
        slim = {
            "kind": pending.get("kind"),
            "warning": pending.get("warning"),
            "event": pending.get("event"),
        }
        lines.append(f"Pending: {json.dumps(slim)}")
    else:
        lines.append("Pending: none")
    return "\n".join(lines)


def parse(message, pending=None):
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[f"{_build_context(pending)}\n\nUser message: {message}"],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.text.strip())
        result = CalendarAction(**json.loads(raw))
        log_event("parse_ok", action=result.action)
        return result
    except Exception as e:
        err_type = type(e).__name__
        err_str = str(e).lower()
        is_quota = (
            "quota" in err_str
            or "429" in err_str
            or "rate" in err_str
            or "resourceexhausted" in err_type.lower()
            or "toomanyrequests" in err_type.lower()
        )
        log_event("parse_error", error_type=err_type, is_quota=is_quota)
        clarification = (
            "The system is momentarily busy — try again in a few seconds."
            if is_quota
            else "Didn't catch that. Rephrase?"
        )
        return CalendarAction(action="clarify", clarification=clarification)