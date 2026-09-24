"""User-timezone-aware 'now'/'today'.

Users pick a UTC offset in Settings (users.tz_offset, whole hours). The bot's
own clock is treated as UTC (the deployment runs UTC), so a user's local time
is simply UTC + offset. Offset 0 reproduces the previous server-time behaviour
exactly, so nothing changes for users who never touch the setting.
"""

import datetime as dt
from typing import Any, Optional


def _offset_hours(user: Any) -> int:
    if user is None:
        return 0
    try:
        return int(user["tz_offset"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def offset_hours(user: Any) -> int:
    """Смещение пользователя в часах — то же, по которому считается user_today.
    Для агрегатов db, принимающих tz_offset: у кого строка уже на руках, тот
    передаёт его и не заставляет каждый агрегат перечитывать users заново."""
    return _offset_hours(user)


def user_now(user: Any) -> dt.datetime:
    """Current wall-clock time in the user's timezone, as a naive datetime."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=_offset_hours(user))


def user_today(user: Any) -> dt.date:
    return user_now(user).date()


def to_user_local(ts: dt.datetime, user: Any) -> dt.datetime:
    """Shift a stored (UTC) timestamp into the user's local wall clock."""
    return ts + dt.timedelta(hours=_offset_hours(user))


def backdated_moment(date: dt.date, tz_offset: int = 0) -> str:
    """Метка времени (наивный UTC, как всё в базе) для записи задним числом за
    календарный день `date` у пользователя с офсетом `tz_offset`.

    Такую метку читают двумя способами, и оба должны увидеть ровно `date`:
    местным днём (db._local_day, приложение переводит UTC в пояс телефона) и
    сырой датой строки (`started_at[:10]`, `logged_at[:10]` в боте и в старых
    сборках приложения). Голый «полдень UTC» удовлетворял только второму: у
    UTC+13/+14 12:00 UTC — это уже 01:00-02:00 следующих суток по местному, и
    тренировка за вторник вставала в историю средой. «Полдень минус офсет»
    (как в handlers.csv_import.apply_import) чинит местный день, но у тех же
    +13/+14 уводит сырую дату во вчера.

    Поэтому середина между местным и UTC-полднем: 12:00 UTC минус половина
    офсета. Местное время выходит 12:00 плюс половина офсета — при любом офсете
    пикера (UTC-11 … UTC+14) и UTC-, и местное время лежат между 05:00 и 19:00
    одних и тех же суток. Офсет 0 даёт прежние 12:00:00.
    """
    noon = dt.datetime.combine(date, dt.time(12, 0))
    return (noon - dt.timedelta(minutes=int(tz_offset) * 30)).isoformat()


def logged_at_for_date(date: Optional[dt.date], tz_offset: int = 0) -> Optional[str]:
    """Метка времени для взвешивания задним числом — см. backdated_moment: не
    полночь и не голый полдень UTC, чтобы запись не свалилась в соседние сутки
    ни в боте (он режет по `logged_at[:10]`), ни в приложении (оно переводит
    метку в пояс телефона).

    Живёт здесь, а не в handlers/bodyweight, потому что задним числом пишет и
    AI-тренер (ai_trainer._log_bodyweight), а импортировать хендлеры оттуда
    нельзя — они сами импортируют ai_trainer.
    """
    if date is None:
        return None
    return backdated_moment(date, tz_offset)
