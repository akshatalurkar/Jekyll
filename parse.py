"""
The only module that talks to Gemini.

One call per inbound message. Pending state is passed in so Gemini
can distinguish "new event" from "correction to pending event" from "yes".
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from models import CalendarAction

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.5-flash"  # cheap parsing tier; swap to flash if you see regressions
TZ = ZoneInfo("America/Los_Angeles")

SYSTEM_PROMPT = """You parse calendar messages into JSON for a WhatsApp calendar assistant. You do NOT chat, explain, translate, summarize, write code, or answer non-calendar questions.

ACTIONS:
- create: user wants to add a new event, OR is correcting fields on a pending event
- update: user wants to change an already-scheduled event (verbs: move, reschedule, change, edit)
- delete: user wants to remove an existing event (verbs: cancel, delete, remove)
- list: user wants events for today, tomorrow, or yesterday
- detail: user wants full info on ONE specific event ("tell me about", "what time is", "details on")
- list_calendars: user asks what calendars they have
- confirm: bare yes (yes, yeah, yep, ok, sure, do it, confirm, correct)
- cancel: bare no (no, nope, never mind, forget it, cancel, stop, don't)
- reject: message is not about calendar/scheduling
- clarify: scheduling intent but missing critical info; put a SHORT question in `clarification`

PENDING STATE RULES:
- If pending state exists and user types event fields without a verb ("4pm", "make it Starbucks", "30 min instead") → action=create. It is a correction.
- "yes" + pending → confirm. "no" + pending → cancel.
- "no, make it 4pm" + pending → create (it's a correction, not a cancel).

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
- "in 2 hours" / "in 30 mins" → compute from current time given in context.
- Spelled-out durations: "thirty minutes"=30, "an hour"=60, "half an hour"=30, "an hour and a half"=90, "two hours"=120, "forty-five minutes"=45. Convert any written number to integer.

TITLE RULES:
- Clean, properly capitalized. Preserve context ("Coffee with Maya", not "Coffee").
- Strip filler: "let's", "gonna", "need to", "gotta", "have to", "remember to".

FIELD RULES:
- duration_minutes: ONLY if user gives one. Null otherwise.
- location: ONLY if explicit. Never infer. To clear an existing location on update, use "".
- calendar: ONLY if explicit ("on my work calendar"). Never infer.

PER ACTION:
- create → fill `event` with whatever fields the user provided.
- update → `target_query` = search keyword for the event. `event` = ONLY the fields that change.
- delete → `target_query` = search keyword.
- detail → `target_query` = search keyword.
- list → `list_date` = YYYY-MM-DD for the day they asked about.

JSON only. No prose, no markdown fences."""


def _build_context(pending: dict | None) -> str:
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
        # Strip noise from pending state before showing Gemini
        slim = {
            "kind": pending.get("kind"),
            "warning": pending.get("warning"),
            "event": pending.get("event"),
        }
        lines.append(f"Pending: {json.dumps(slim)}")
    else:
        lines.append("Pending: none")
    return "\n".join(lines)


def parse(message: str, pending: dict | None = None) -> CalendarAction:
    """Returns a validated CalendarAction. Never raises."""
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[f"{_build_context(pending)}\n\nUser message: {message}"],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        raw = response.text.strip().replace("```json", "").replace("```", "")
        return CalendarAction(**json.loads(raw))
    except Exception as e:
        print(f"[parse error] {type(e).__name__}: {e}")
        return CalendarAction(
            action="clarify",
            clarification="Didn't catch that. Rephrase?",
        )