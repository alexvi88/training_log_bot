"""Утренний разбор поведения: что админ получает вместе с суточным отчётом."""

import datetime as dt
from unittest.mock import AsyncMock

import pytest

import activity_log
import admin_tasks
import config
import db

pytestmark = pytest.mark.asyncio

MSK = 3


async def _event(user_id: int, kind: str, content: str, at: str) -> None:
    await db.log_user_event(user_id, kind, content)
    await db.conn().execute(
        "UPDATE user_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM user_events)",
        (at,),
    )
    await db.conn().commit()


def _utc_for_msk(day: dt.date, hour: int, minute: int = 0) -> str:
    """Момент московских суток в том виде, в каком он лежит в базе (UTC)."""
    return (dt.datetime.combine(day, dt.time(hour, minute)) - dt.timedelta(hours=MSK)).isoformat(
        timespec="seconds"
    )


async def test_summary_groups_the_day_by_person_and_keeps_the_coach_side(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", MSK)
    day = dt.date(2026, 8, 17)
    await fresh_db.get_or_create_user(111, "athlete")
    await _event(111, activity_log.KIND_MESSAGE, "хочу программу", _utc_for_msk(day, 9))
    await _event(111, activity_log.KIND_AI_REPLY, "Собрал тебе верх/низ", _utc_for_msk(day, 9, 1))
    await _event(111, activity_log.KIND_CALLBACK, "✅ Добавить себе", _utc_for_msk(day, 9, 2))

    summary = await admin_tasks.build_behaviour_summary(day)

    assert "17.08.2026" in summary
    assert "Действий: 3, людей: 1" in summary
    assert "@athlete" in summary
    # Своя сторона разговора — та самая, которой в ленте не было.
    assert "🤖 Собрал тебе верх/низ" in summary
    # Время московское: событие 09:00 МСК записано в базу как 06:00 UTC.
    assert "09:00 💬 хочу программу" in summary


async def test_summary_takes_the_admins_day_not_the_utc_one(fresh_db, monkeypatch):
    """Вечер по Москве — это ещё вчера, хотя в UTC он уже сегодня."""
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", MSK)
    day = dt.date(2026, 8, 17)
    await fresh_db.get_or_create_user(111, "athlete")
    await _event(111, activity_log.KIND_MESSAGE, "вечерняя тренировка", _utc_for_msk(day, 23, 30))
    await _event(111, activity_log.KIND_MESSAGE, "утро следующего дня", _utc_for_msk(day + dt.timedelta(days=1), 0, 30))

    summary = await admin_tasks.build_behaviour_summary(day)

    assert "вечерняя тренировка" in summary
    assert "утро следующего дня" not in summary


async def test_empty_day_costs_nothing(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", MSK)
    assert await admin_tasks.build_behaviour_summary(dt.date(2026, 8, 17)) is None


async def test_digest_is_sent_to_the_admin_as_its_own_message(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", MSK)
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    day = dt.date(2026, 8, 17)
    await fresh_db.get_or_create_user(111, "athlete")
    await _event(111, activity_log.KIND_MESSAGE, "привет", _utc_for_msk(day, 10))

    seen: list[str] = []

    async def fake_digest(summary: str) -> str:
        seen.append(summary)
        return "Путь один: пришёл, спросил, ушёл."

    monkeypatch.setattr(admin_tasks.ai_trainer, "behaviour_digest", fake_digest)
    bot = AsyncMock()

    await admin_tasks._send_behaviour_digest(bot, day)

    assert seen, "модель должна получить материал за сутки"
    (call,) = bot.send_message.call_args_list
    assert call.kwargs["chat_id"] == 999
    assert "Как вчера пользовались — 17.08.2026" in call.kwargs["text"]
    assert "Путь один" in call.kwargs["text"]


async def test_nothing_is_sent_when_the_model_stays_silent(fresh_db, monkeypatch):
    """Молчание модели (нет ключа, деньги кончились, сбой) — не повод будить
    админа пустым сообщением каждое утро."""
    monkeypatch.setattr(config, "ADMIN_TZ_OFFSET", MSK)
    monkeypatch.setattr(config, "ADMIN_ID", 999)
    day = dt.date(2026, 8, 17)
    await fresh_db.get_or_create_user(111, "athlete")
    await _event(111, activity_log.KIND_MESSAGE, "привет", _utc_for_msk(day, 10))
    monkeypatch.setattr(admin_tasks.ai_trainer, "behaviour_digest", AsyncMock(return_value=None))
    bot = AsyncMock()

    await admin_tasks._send_behaviour_digest(bot, day)

    bot.send_message.assert_not_awaited()
