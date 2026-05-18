from datetime import datetime, timedelta, timezone
from .core import db

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