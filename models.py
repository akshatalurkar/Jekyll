"""
Schema for everything Gemini is allowed to return.
Confidence is intentionally absent: the action enum + clarify path +
pending-state confirmation flow handles uncertainty already.
"""

from pydantic import BaseModel
from typing import Literal, Optional

Action = Literal[
    "create",          # new event, OR a correction to a pending event
    "update",          # modify an already-scheduled event
    "delete",          # remove an already-scheduled event
    "list",            # show events for today/tomorrow/yesterday
    "detail",          # show full info for one event
    "list_calendars",  # show user's calendar names
    "confirm",         # bare yes
    "cancel",          # bare no
    "reject",          # not a calendar request
    "clarify",         # scheduling intent but missing info
]


class EventFields(BaseModel):
    """All event fields. Used for create payloads and update deltas."""
    title: Optional[str] = None
    date: Optional[str] = None              # YYYY-MM-DD
    time: Optional[str] = None              # HH:MM (24-hour)
    duration_minutes: Optional[int] = None
    location: Optional[str] = None          # use "" to explicitly clear on update
    calendar: Optional[str] = None          # calendar name hint


class CalendarAction(BaseModel):
    action: Action
    event: Optional[EventFields] = None     # create payload / update deltas
    target_query: Optional[str] = None      # update / delete / detail: search keyword
    list_date: Optional[str] = None         # list: YYYY-MM-DD of requested day
    clarification: Optional[str] = None     # short question for action=clarify