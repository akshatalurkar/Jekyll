"""
Pending-state helpers. Backs onto User.last_event in the DB.

Pending state shape:
{
  "kind": "create" | "update" | "delete",
  "warning": "past" | "now" | "conflict" | null,    # only for create
  "event": {                                         # the event being created/updated
    "title", "date", "time", "duration_minutes",
    "location", "calendar_id", "calendar_name",
    ...
  },
  "event_id": str,            # for update/delete: the Google Calendar event ID
  "original": {...},          # for update/delete: snapshot of original event for diff display
  "conflict": {...},          # for create with warning=conflict: brief info on the conflict
}
"""

from datetime import datetime, timedelta, timezone
from core import db

PENDING_TTL_MINUTES = 10


def set_pending(db, user, payload: dict | None) -> None:
    user.last_event = payload
    user.last_event_updated_at = datetime.now(timezone.utc) if payload else None
    db.session.commit()


def get_pending(user) -> dict | None:
    if not user.last_event:
        return None
    return user.last_event


def is_stale(user) -> bool:
    if not user.last_event_updated_at:
        return False
    age = datetime.now(timezone.utc) - user.last_event_updated_at.replace(tzinfo=timezone.utc)
    return age > timedelta(minutes=PENDING_TTL_MINUTES)


def clear_pending(db, user) -> None:
    set_pending(db, user, None)