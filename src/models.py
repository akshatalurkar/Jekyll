from pydantic import BaseModel
from typing import Literal, Optional

Action = Literal[
    "create",
    "update",
    "delete",
    "list",
    "detail",
    "list_calendars",
    "refresh",
    "confirm",
    "cancel",
    "reject",
    "clarify",
]


class EventFields(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    duration_minutes: Optional[int] = None
    location: Optional[str] = None
    calendar: Optional[str] = None
    reminder_minutes: Optional[int] = None
    reminder_at: Optional[str] = None


class CalendarAction(BaseModel):
    action: Action
    event: Optional[EventFields] = None
    target_query: Optional[str] = None
    list_date: Optional[str] = None
    clarification: Optional[str] = None