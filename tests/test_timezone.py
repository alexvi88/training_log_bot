"""User timezone: timeutil helpers, the settings picker, and persistence."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import keyboards
import timeutil
from handlers import settings


def test_user_now_shifts_by_offset():
    base = timeutil.user_now({"tz_offset": 0})
    plus3 = timeutil.user_now({"tz_offset": 3})
    # ~3h apart (allow a second of wall-clock drift between the two calls).
    assert abs((plus3 - base) - dt.timedelta(hours=3)) < dt.timedelta(seconds=2)


def test_missing_offset_defaults_to_zero():
    assert timeutil._offset_hours(None) == 0
    assert timeutil._offset_hours({}) == 0


def test_to_user_local():
    ts = dt.datetime(2026, 7, 23, 22, 0, 0)
    assert timeutil.to_user_local(ts, {"tz_offset": 3}) == dt.datetime(2026, 7, 24, 1, 0, 0)


def test_format_utc_offset():
    assert keyboards.format_utc_offset(0) == "UTC"
    assert keyboards.format_utc_offset(3) == "UTC+3"
    assert keyboards.format_utc_offset(-1) == "UTC-1"


def test_picker_marks_current_and_has_all_offsets():
    kb = keyboards.timezone_picker_keyboard(3)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "settings:tzset:-1" in cbs
    assert "settings:tzset:12" in cbs
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "• UTC+3 •" in texts


def _callback(user_id, data):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=user_id, username="t")
    cb.message = message
    cb.data = data
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_setting_timezone_persists(fresh_db, user_id):
    db = fresh_db
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    cb = _callback(user_id, "settings:tzset:5")

    await settings.settings_timezone_set(cb, state)

    user = await db.get_user(user_id)
    assert user["tz_offset"] == 5


# ---------- the daily push job sends at each user's own hour ----------


def test_local_now_shifts_by_the_user_offset(monkeypatch):
    import engagement

    utc_now = dt.datetime(2026, 7, 27, 15, 0)  # 15:00 UTC
    monkeypatch.setattr(engagement, "_utc_now", lambda: utc_now)

    assert engagement._local_now(0).hour == 15
    assert engagement._local_now(5).hour == 20
    assert engagement._local_now(-3).hour == 12


def test_is_send_hour_uses_the_user_offset(monkeypatch):
    import engagement

    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 27, 15, 0))

    assert engagement.is_send_hour(0, 15) is True     # UTC user: it's 15:00
    assert engagement.is_send_hour(5, 15) is False    # UTC+5: it's 20:00 for them
    assert engagement.is_send_hour(5, 20) is True
    assert engagement.is_send_hour(-3, 12) is True    # UTC-3: it's 12:00


async def test_push_job_skips_users_whose_local_hour_has_not_come(fresh_db, user_id, monkeypatch):
    """Everyone used to be pushed at the server's ENGAGEMENT_HOUR, so a user five
    zones over got their evening nudge mid-afternoon."""
    import config
    import engagement

    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )
    await db.update_user(user_id, tz_offset=5)

    built = []

    async def fake_build(telegram_id, today):
        built.append((telegram_id, today))
        return None

    monkeypatch.setattr(engagement, "build_daily_push", fake_build)
    monkeypatch.setattr(engagement, "is_send_hour", lambda tz, hour: tz == 5 and hour == config.ENGAGEMENT_HOUR)

    bot = MagicMock()
    await engagement._send_daily_pushes(bot)

    assert [uid for uid, _ in built] == [user_id]


async def test_push_job_passes_the_users_local_date(fresh_db, user_id, monkeypatch):
    """has_push_today is keyed on the date it's given, so the one-per-day promise
    has to be evaluated against the user's own day, not the server's."""
    import engagement

    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )
    await db.update_user(user_id, tz_offset=5)

    seen = []

    async def fake_build(telegram_id, today):
        seen.append(today)
        return None

    monkeypatch.setattr(engagement, "build_daily_push", fake_build)
    monkeypatch.setattr(engagement, "is_send_hour", lambda tz, hour: True)
    monkeypatch.setattr(engagement, "_local_now", lambda tz: dt.datetime(2026, 7, 28, 1, 0))

    await engagement._send_daily_pushes(MagicMock())

    assert seen == [dt.date(2026, 7, 28)]


# ---------- date pickers show the user's today ----------


def test_calendar_marks_the_given_today_not_the_servers():
    today = dt.date(2026, 7, 28)
    kb = keyboards.calendar_keyboard("bf", 2026, 7, today=today)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "·28·" in labels           # marked as today
    assert "29" not in labels         # the day after is not selectable
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bf:date:2026-07-28" in cbs
    assert "bf:date:2026-07-29" not in cbs


def test_quick_dates_follow_the_given_today():
    kb = keyboards.date_quick_keyboard("bf", today=dt.date(2026, 7, 28))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bf:date:2026-07-28" in cbs  # Сегодня
    assert "bf:date:2026-07-27" in cbs  # Вчера
    assert "bf:date:2026-07-26" in cbs  # Позавчера


def test_parse_ru_date_accepts_the_users_today():
    """At UTC+13 the user's today can be the server's tomorrow — typing their own
    date shouldn't come back as "дата в будущем"."""
    from parser import ParseError, parse_ru_date

    users_today = dt.date(2026, 7, 28)
    assert parse_ru_date("28.07.2026", today=users_today) == users_today
    with pytest.raises(ParseError):
        parse_ru_date("29.07.2026", today=users_today)
