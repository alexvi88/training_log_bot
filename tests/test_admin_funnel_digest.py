"""Еженедельная сводка воронки новичка — владельцу, встроенная в тот же
часовой тик, что и обычные пуши (см. engagement._maybe_send_admin_funnel_digest).
"""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import db
import engagement

pytestmark = pytest.mark.asyncio

MONDAY_MORNING = dt.datetime(2026, 8, 3, engagement.ADMIN_DIGEST_HOUR, 0)  # понедельник
assert MONDAY_MORNING.weekday() == engagement.ADMIN_DIGEST_WEEKDAY


async def _admin(fresh_db, admin_id: int = 999, tz_offset: int = 0):
    await fresh_db.get_or_create_user(admin_id, "owner")
    await fresh_db.update_user(admin_id, tz_offset=tz_offset)
    # Легаси-источник — вне воронки навсегда (см. acquisition.SOURCE_LEGACY):
    # без этого сам админ, только что заведённый фикстурой, попадал бы в
    # свою же еженедельную сводку как «новый», искажая числа теста.
    import acquisition
    await fresh_db.set_user_source(admin_id, acquisition.SOURCE_LEGACY)
    return admin_id


async def test_digest_goes_out_only_to_the_admin_on_monday_morning(fresh_db, monkeypatch):
    admin_id = await _admin(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", admin_id)
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await engagement._maybe_send_admin_funnel_digest(bot)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == admin_id


async def test_digest_is_silent_without_an_admin_id(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await engagement._maybe_send_admin_funnel_digest(bot)

    bot.send_message.assert_not_awaited()


async def test_digest_is_silent_outside_monday_morning(fresh_db, monkeypatch):
    admin_id = await _admin(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", admin_id)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    # Тот же час, но вторник.
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING + dt.timedelta(days=1))
    await engagement._maybe_send_admin_funnel_digest(bot)
    bot.send_message.assert_not_awaited()

    # Понедельник, но не тот час.
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING.replace(hour=engagement.ADMIN_DIGEST_HOUR + 3))
    await engagement._maybe_send_admin_funnel_digest(bot)
    bot.send_message.assert_not_awaited()


async def test_digest_numbers_match_the_db_function(fresh_db, monkeypatch):
    admin_id = await _admin(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", admin_id)
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING)

    newbie = (await fresh_db.get_or_create_user(111, "newbie"))["telegram_id"]
    await db.set_user_source(newbie, "src_a")
    group = await fresh_db.create_muscle_group(newbie, "Грудь")
    ex_id = await fresh_db.create_exercise(newbie, "Жим", group)
    workout_id = await fresh_db.create_workout(newbie)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 60.0, 8)
    await fresh_db.finish_workout(workout_id)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await engagement._maybe_send_admin_funnel_digest(bot)

    rows = await db.onboarding_funnel(engagement.ADMIN_DIGEST_LOOKBACK_DAYS)
    total_finished = sum(r["finished"] for r in rows)
    text = bot.send_message.await_args.args[1]
    assert f"{total_finished} завершили первую" in text


async def test_digest_sends_once_a_week_not_twice(fresh_db, monkeypatch):
    admin_id = await _admin(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", admin_id)
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await engagement._maybe_send_admin_funnel_digest(bot)
    await engagement._maybe_send_admin_funnel_digest(bot)  # тот же тик/тот же понедельник

    bot.send_message.assert_awaited_once()

    # Через час, всё ещё понедельник, но тот же час уже был обработан —
    # реалистичнее: следующий тик приходит через час, час уже не тот.
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING + dt.timedelta(hours=1))
    await engagement._maybe_send_admin_funnel_digest(bot)
    bot.send_message.assert_awaited_once()  # всё ещё один раз — не тот час

    # Следующий понедельник — новая сводка.
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING + dt.timedelta(days=7))
    await engagement._maybe_send_admin_funnel_digest(bot)
    assert bot.send_message.await_count == 2


async def test_digest_is_honest_about_zero_newcomers(fresh_db, monkeypatch):
    admin_id = await _admin(fresh_db)
    monkeypatch.setattr(config, "ADMIN_ID", admin_id)
    monkeypatch.setattr(engagement, "_utc_now", lambda: MONDAY_MORNING)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await engagement._maybe_send_admin_funnel_digest(bot)

    text = bot.send_message.await_args.args[1]
    assert "не пришло" in text
