"""Wow-разбор сразу после CSV/Hevy-импорта: db.exercise_history_spans (факты) и
ai_trainer.import_history_overview (что тренер об этом скажет).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ai_trainer

pytestmark = pytest.mark.asyncio


def _completion(text):
    message = SimpleNamespace(content=text)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])


def _client(create) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def _log(db, user_id, exercise_id, day, weight=100.0, reps=5):
    workout_id = await db.create_finished_workout(
        user_id, f"{day}T12:00:00", f"{day}T12:30:00"
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, 1, 0, weight, reps)
    return workout_id


# ---------- db.exercise_history_spans ----------


async def test_exercise_history_spans_counts_sessions_and_dates(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group)
    for i in range(5):
        await _log(db, user_id, squat, f"2026-01-{10 + i:02d}")

    spans = await db.exercise_history_spans(user_id)

    assert len(spans) == 1
    row = spans[0]
    assert row["display_name"] == "Присед"
    assert row["group_name"] == "Ноги"
    assert row["sessions"] == 5
    assert row["first_at"] == "2026-01-10"
    assert row["last_at"] == "2026-01-14"


async def test_exercise_history_spans_orders_by_session_count(fresh_db, user_id):
    """Модели дают самые частые упражнения первыми — их и обрезает
    _IMPORT_OVERVIEW_TOP_EXERCISES на большой истории."""
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Грудь")
    often = await db.create_exercise(user_id, "Жим", group)
    rare = await db.create_exercise(user_id, "Разводка", group)
    for i in range(5):
        await _log(db, user_id, often, f"2026-01-{10 + i:02d}")
    await _log(db, user_id, rare, "2026-01-10")

    spans = await db.exercise_history_spans(user_id)

    assert [row["display_name"] for row in spans] == ["Жим", "Разводка"]


# ---------- ai_trainer.import_history_overview ----------


async def test_no_overview_below_the_minimum_workout_count(fresh_db, user_id, monkeypatch):
    """Одна-две только что записанные тренировки — не история, разбирать нечего."""
    db = fresh_db
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим", group)
    await _log(db, user_id, bench, "2026-08-01")
    await _log(db, user_id, bench, "2026-08-03")

    create = AsyncMock()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _client(create))

    result = await ai_trainer.import_history_overview(user_id)

    assert result is None
    create.assert_not_called()


async def test_no_overview_when_ai_is_not_configured(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)

    assert await ai_trainer.import_history_overview(user_id) is None


async def test_overview_flags_a_habit_gone_quiet(fresh_db, user_id, monkeypatch):
    """Присед — привычка (5 раз), брошенная задолго до конца истории — должен
    попасть в «давно не встречались»; жим идёт до самого конца и не попадает."""
    db = fresh_db
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    legs = await db.create_muscle_group(user_id, "Ноги")
    chest = await db.create_muscle_group(user_id, "Грудь")
    squat = await db.create_exercise(user_id, "Присед", legs)
    bench = await db.create_exercise(user_id, "Жим", chest)
    for i in range(5):
        await _log(db, user_id, squat, f"2026-01-{10 + i:02d}")
    for i in range(5):
        await _log(db, user_id, bench, f"2026-07-{1 + i:02d}")

    captured = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _completion("Вижу тебя.")

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: _client(AsyncMock(side_effect=fake_create))
    )

    result = await ai_trainer.import_history_overview(user_id)

    assert result == "Вижу тебя."
    summary = captured["messages"][1]["content"]
    assert "Всего тренировок в истории: 10." in summary
    assert "Присед" in summary and "Жим" in summary
    assert "Давно не встречались" in summary
    assert "Присед: последний раз 2026-01-14" in summary
    assert "Жим" not in summary.split("Давно не встречались")[1]


async def test_overview_returns_none_on_model_failure(fresh_db, user_id, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим", group)
    for i in range(3):
        await _log(db, user_id, bench, f"2026-08-{1 + i:02d}")

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: _client(AsyncMock(side_effect=RuntimeError("boom")))
    )

    assert await ai_trainer.import_history_overview(user_id) is None
