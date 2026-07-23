"""User-timezone-aware 'now'/'today'.

Users pick a UTC offset in Settings (users.tz_offset, whole hours). The bot's
own clock is treated as UTC (the deployment runs UTC), so a user's local time
is simply UTC + offset. Offset 0 reproduces the previous server-time behaviour
exactly, so nothing changes for users who never touch the setting.
"""

import datetime as dt
from typing import Any


def _offset_hours(user: Any) -> int:
    if user is None:
        return 0
    try:
        return int(user["tz_offset"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def user_now(user: Any) -> dt.datetime:
    """Current wall-clock time in the user's timezone, as a naive datetime."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=_offset_hours(user))


def user_today(user: Any) -> dt.date:
    return user_now(user).date()


def to_user_local(ts: dt.datetime, user: Any) -> dt.datetime:
    """Shift a stored (UTC) timestamp into the user's local wall clock."""
    return ts + dt.timedelta(hours=_offset_hours(user))
