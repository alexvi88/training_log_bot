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


@pytest.mark.asyncio
async def test_setting_timezone_is_a_toast_not_a_modal(fresh_db, user_id):
    """«Часовой пояс: +3» за модалкой с ОК — лишний тап ради одной цифры;
    show_alert=False убирает кнопку, а не сам текст."""
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    cb = _callback(user_id, "settings:tzset:5")

    await settings.settings_timezone_set(cb, state)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.call_args
    assert "UTC+5" in args[0]
    assert kwargs.get("show_alert") is False


@pytest.mark.asyncio
async def test_setting_timezone_marks_it_as_chosen_by_the_user(fresh_db, user_id):
    """Отдельный флаг от значения tz_offset: у новичка оно и так не ноль (см.
    config.DEFAULT_TZ_OFFSET), так что подсказка под пушем (engagement.
    _should_show_tz_hint) должна опираться на факт выбора, а не на цифру."""
    db = fresh_db
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    assert (await db.get_user(user_id))["tz_set_by_user"] == 0

    await settings.settings_timezone_set(_callback(user_id, "settings:tzset:5"), state)

    assert (await db.get_user(user_id))["tz_set_by_user"] == 1


# ---------- дефолтный пояс нового атлета ----------


@pytest.mark.asyncio
async def test_new_user_defaults_to_utc_plus_3(fresh_db):
    """Владелец подтвердил смену дефолта: аудитория русскоязычная, и Москва
    (UTC+3) — куда более частая точка старта, чем UTC. Существующих
    пользователей это не трогает (см. test_push_job_leaves_users_without_a_
    timezone_alone_at_night) — меняется только значение для НОВОЙ записи."""
    import config

    row = await fresh_db.get_or_create_user(telegram_id=777, username="newbie")

    assert row["tz_offset"] == config.DEFAULT_TZ_OFFSET == 3
    assert row["tz_set_by_user"] == 0  # значение по умолчанию, не осознанный выбор


# ---------- подсказка про пояс под первым пушем ----------


@pytest.mark.asyncio
async def test_first_push_offers_the_timezone_hint(fresh_db, user_id):
    """Под первым же пушем — строка «пуш пришёл не вовремя? скажи пояс», с
    кнопкой в существующий пикер настроек."""
    import engagement
    import push_texts
    from tests.test_push_delivery import _bot

    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "текст")

    await engagement._deliver(bot, user_id, decision, dt.date(2026, 5, 4))

    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    rows = kb.inline_keyboard
    assert any(b.callback_data == "settings:tz" for row in rows for b in row)
    assert (await fresh_db.get_user(user_id))["tz_push_hint_shown"] == 1


@pytest.mark.asyncio
async def test_second_push_does_not_repeat_the_timezone_hint(fresh_db, user_id):
    import engagement
    import push_texts
    from tests.test_push_delivery import _bot

    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "текст")

    await engagement._deliver(bot, user_id, decision, dt.date(2026, 5, 4))
    await engagement._deliver(bot, user_id, decision, dt.date(2026, 5, 5))

    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    rows = kb.inline_keyboard
    assert not any(b.callback_data == "settings:tz" for row in rows for b in row)


@pytest.mark.asyncio
async def test_timezone_hint_is_not_shown_to_a_user_who_already_set_it(fresh_db, user_id):
    import engagement
    import push_texts
    from tests.test_push_delivery import _bot

    await fresh_db.mark_tz_set_by_user(user_id)
    bot = _bot()
    decision = engagement.PushDecision(push_texts.SKIP_3, "текст")

    await engagement._deliver(bot, user_id, decision, dt.date(2026, 5, 4))

    kb = bot.send_photo.await_args.kwargs["reply_markup"]
    rows = kb.inline_keyboard
    assert not any(b.callback_data == "settings:tz" for row in rows for b in row)


# ---------- the daily push job sends at each user's own hour ----------


def test_local_now_shifts_by_the_user_offset(monkeypatch):
    import engagement

    utc_now = dt.datetime(2026, 7, 27, 15, 0)  # 15:00 UTC
    monkeypatch.setattr(engagement, "_utc_now", lambda: utc_now)

    assert engagement._local_now(0).hour == 15
    assert engagement._local_now(5).hour == 20
    assert engagement._local_now(-3).hour == 12


def test_should_send_now_uses_the_user_offset(monkeypatch):
    import engagement

    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 27, 15, 0))

    assert engagement.should_send_now(5, 15) is False    # UTC+5: it's 20:00 for them
    assert engagement.should_send_now(5, 20) is True
    assert engagement.should_send_now(-3, 12) is True    # UTC-3: it's 12:00


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
    monkeypatch.setattr(engagement, "should_send_now", lambda tz, hour: tz == 5 and hour == config.ENGAGEMENT_HOUR)

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
    monkeypatch.setattr(engagement, "should_send_now", lambda tz, hour: True)
    monkeypatch.setattr(engagement, "_local_now", lambda tz: dt.datetime(2026, 7, 28, 1, 0))

    await engagement._send_daily_pushes(MagicMock())

    assert seen == [dt.date(2026, 7, 28)]


async def test_push_job_leaves_users_without_a_timezone_alone_at_night(fresh_db, user_id, monkeypatch):
    """tz_offset = 0 — это «не знаем», а не «UTC» (см. engagement.tz_is_known).
    Новый атлет с этим больше не сталкивается — get_or_create_user пишет ему
    config.DEFAULT_TZ_OFFSET (см. test_new_user_defaults_to_utc_plus_3), но у
    пользователя, заведённого до этой миграции, ноль остаётся, и на сервере
    19:00 у него может быть и два ночи — тик его не трогает: ни отправки, ни
    даже сборки пуша (а по воскресеньям сборка — это ещё и вызов модели)."""
    import engagement

    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
    )
    # Симулируем пользователя времён прежнего дефолта: пояс не трогал, и он
    # остался нулём — в отличие от новых аккаунтов, которые дефолт больше не
    # затрагивает.
    await db.update_user(user_id, tz_offset=0)

    built = []

    async def fake_build(telegram_id, today):
        built.append(telegram_id)
        return None

    monkeypatch.setattr(engagement, "build_daily_push", fake_build)
    monkeypatch.setattr(engagement, "_utc_now", lambda: dt.datetime(2026, 7, 27, 19, 0))

    await engagement._send_daily_pushes(MagicMock())
    assert built == []

    # ...а в безопасный для всех поясов час — трогает.
    monkeypatch.setattr(
        engagement, "_utc_now",
        lambda: dt.datetime(2026, 7, 27, engagement.UNKNOWN_TZ_SEND_HOUR_UTC, 0),
    )
    await engagement._send_daily_pushes(MagicMock())
    assert built == [user_id]


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


def test_parse_ru_date_accepts_the_users_today():
    """At UTC+13 the user's today can be the server's tomorrow — typing their own
    date shouldn't come back as "дата в будущем"."""
    from parser import ParseError, parse_ru_date

    users_today = dt.date(2026, 7, 28)
    assert parse_ru_date("28.07.2026", today=users_today) == users_today
    with pytest.raises(ParseError):
        parse_ru_date("29.07.2026", today=users_today)


async def test_slow_tick_still_pushes_users_resolved_at_its_start(fresh_db, user_id, monkeypatch):
    """Building a push can be slow — Sunday's digest makes an LLM call per user
    — so a long tick can outlast the hour it started in. Re-checking the clock
    per user as the loop crawled dropped everyone near the end of the list, and
    by the next tick their send hour had passed: the push was lost, not late."""
    import engagement

    db = fresh_db
    other = (await db.get_or_create_user(telegram_id=222, username="second"))["telegram_id"]
    for uid in (user_id, other):
        await db.create_finished_workout(
            uid, started_at="2026-07-01T10:00:00", finished_at="2026-07-01T11:00:00"
        )

    clock = {"hour_has_passed": False}

    def fake_should_send_now(tz, hour):
        return not clock["hour_has_passed"]

    built = []

    async def slow_build(telegram_id, today):
        built.append(telegram_id)
        clock["hour_has_passed"] = True  # the first user's push took us past the hour
        return None

    monkeypatch.setattr(engagement, "should_send_now", fake_should_send_now)
    monkeypatch.setattr(engagement, "build_daily_push", slow_build)

    await engagement._send_daily_pushes(MagicMock())

    assert sorted(built) == sorted([user_id, other])
