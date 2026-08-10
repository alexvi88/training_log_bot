"""Повторная загрузка того же CSV.

Проверки на дубли в импорте не было вовсе: присланный второй раз файл молча
удваивал историю — 20 тренировок и 400 подходов становились 40 и 800, а
пересчёт ачивок закреплял результат по удвоенному тоннажу. Разгребать
приходилось руками, удаляя тренировки по одной. Триггер бытовой: человек не
понял, дошёл ли файл, и прислал его ещё раз.

Молча пропускать дубли — не лучше: экран подтверждения обязан показывать, что
именно загрузится, и оставлять возможность настоять на своём.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow

TWO_DAYS = [
    {"date": "2026-05-04", "entries": [{"name": "Присед", "sets": [(100.0, 5, None), (100.0, 5, None)]}]},
    {"date": "2026-05-06", "entries": [{"name": "Присед", "sets": [(105.0, 5, None)]}]},
]


def _callback(user_id: int, data: str = "imp:save"):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=user_id))
    )
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = message
    return callback


async def _state(user_id: int, workouts, ex_id: int) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(ImportFlow.confirming)
    await state.update_data(imp_workouts=workouts, imp_resolved={"Присед": ex_id})
    return state


async def _totals(db, user_id: int) -> tuple[int, int]:
    workouts = await db.count_workouts(user_id)
    cur = await db.conn().execute("SELECT COUNT(*) FROM sets")
    (sets,) = await cur.fetchone()
    return workouts, sets


@pytest.fixture
def alerts(monkeypatch):
    """Импорт рапортует алертом поверх перерисованных настроек — ловим его."""
    seen: list[str] = []

    async def fake_show_settings(event, state, alert=None):
        seen.append(alert)

    monkeypatch.setattr("handlers.settings.show_settings", fake_show_settings)
    return seen


@pytest.fixture
async def squat(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    return await fresh_db.create_exercise(user_id, "Присед", gid)


async def test_save_shows_a_progress_message_before_writing(fresh_db, user_id, squat, alerts):
    """Запись подходов по одному плюс пересчёт ачивок — не мгновенно на файле
    из нескольких тренировок, а кнопка до этого момента ничем не показывала,
    что вообще что-то происходит."""
    callback = _callback(user_id)

    await csv_import.import_save(callback, await _state(user_id, TWO_DAYS, squat))

    calls = [
        c.args[0] for mock in (callback.message.edit_text, callback.message.answer)
        for c in mock.await_args_list if c.args
    ]
    assert any("Загружаю" in t for t in calls), calls


async def test_sending_the_same_file_twice_does_not_double_the_history(
    fresh_db, user_id, squat, alerts
):
    db = fresh_db
    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat))
    after_first = await _totals(db, user_id)
    assert after_first == (2, 3)

    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat))

    assert await _totals(db, user_id) == after_first
    assert "уже есть" in alerts[-1]


async def test_overlapping_file_imports_only_the_new_dates(fresh_db, user_id, squat, alerts):
    db = fresh_db
    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS[:1], squat))

    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat))

    assert await _totals(db, user_id) == (2, 3)
    dates = await db.list_finished_workout_dates(user_id)
    assert dates == ["2026-05-04", "2026-05-06"]
    assert "Импортировано 1 тренировка" in alerts[-1]
    assert "пропущено 1" in alerts[-1]


async def test_double_tap_on_save_does_not_import_the_file_twice(
    fresh_db, user_id, squat, alerts
):
    """Живой сценарий: два колбэка «Загрузить» почти одновременно (двойной тап,
    или Telegram, повторно доставивший тот же апдейт) — раньше оба видели
    одинаковый ImportFlow.confirming и оба независимо писали файл в базу."""
    import asyncio

    db = fresh_db

    await asyncio.gather(
        csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat)),
        csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat)),
    )

    assert await _totals(db, user_id) == (2, 3)


async def test_a_failing_workout_does_not_abort_the_rest_of_the_import(
    fresh_db, user_id, squat, alerts, monkeypatch
):
    """Одна тренировка сломалась посреди записи (DB-ошибка, необработанное
    исключение) — соседние в том же файле не должны от этого пострадать, а
    сломанная не должна остаться в базе наполовину записанной."""
    db = fresh_db
    real_add_set = db.add_set
    calls = {"n": 0}

    async def flaky_add_set(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # оба подхода первой тренировки прошли, это уже вторая
            raise RuntimeError("boom")
        return await real_add_set(*args, **kwargs)

    monkeypatch.setattr(db, "add_set", flaky_add_set)

    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat))

    # Первая тренировка (2 подхода) успела записаться целиком, вторая — нет ни капли.
    assert await _totals(db, user_id) == (1, 2)
    assert "не получилось" in alerts[-1]


async def test_load_everything_button_still_allows_a_deliberate_duplicate(
    fresh_db, user_id, squat, alerts
):
    """Две тренировки в один день бывают — но только по явной кнопке."""
    db = fresh_db
    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS[:1], squat))

    await csv_import.import_save(
        _callback(user_id, "imp:saveall"), await _state(user_id, TWO_DAYS[:1], squat)
    )

    assert await _totals(db, user_id) == (2, 4)
    assert "пропущено" not in alerts[-1]


# ---------- экран подтверждения ----------


def _message_event(user_id: int):
    event = MagicMock()
    event.from_user = SimpleNamespace(id=user_id, username="tester")
    event.answer = AsyncMock()
    return event


async def _confirmation(user_id: int, workouts, ex_id: int, page: int = 0):
    event = _message_event(user_id)
    state = await _state(user_id, workouts, ex_id)
    await csv_import.show_confirmation(event, state)
    if page:
        # _render_confirmation_page treats a plain Message the same as the
        # first page — simplest way to look at another page in a test
        # without wiring up the full CallbackQuery/ui.safe_edit machinery.
        event = _message_event(user_id)
        await csv_import._render_confirmation_page(event, state, page)
    call = event.answer.await_args
    text = call.args[0] if call.args else call.kwargs["text"]
    kb = call.kwargs["reply_markup"]
    return text, [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_confirmation_names_the_dates_that_are_already_in_history(
    fresh_db, user_id, squat, alerts
):
    """Дубль (04.05.2026) отмечен прямо у своей даты в списке, а не общей
    фразой поверх всего файла — у соседней (новой) даты отметки нет."""
    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS[:1], squat))

    text, buttons = await _confirmation(user_id, TWO_DAYS, squat)

    assert "04.05.2026" in text and "06.05.2026" in text
    assert "уже есть в истории" in text
    lines = text.splitlines()
    dup_line = next(line for line in lines if "04.05.2026" in line)
    fresh_line = next(line for line in lines if "06.05.2026" in line)
    assert "уже есть" in dup_line
    assert "уже есть" not in fresh_line
    assert "imp:save" in buttons and "imp:saveall" in buttons


async def test_confirmation_of_a_fully_duplicate_file_has_no_plain_load_button(
    fresh_db, user_id, squat, alerts
):
    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS, squat))

    text, buttons = await _confirmation(user_id, TWO_DAYS, squat)

    assert "уже есть в истории" in text
    assert "imp:save" not in buttons            # нечего загружать «как обычно»
    assert "imp:saveall" in buttons             # но настоять всё ещё можно
    assert "imp:cancel" in buttons


async def test_confirmation_without_duplicates_looks_as_before(fresh_db, user_id, squat):
    text, buttons = await _confirmation(user_id, TWO_DAYS, squat)

    assert "уже есть" not in text
    assert "imp:save" in buttons and "imp:saveall" not in buttons


async def test_same_day_different_exercise_is_not_treated_as_a_duplicate(
    fresh_db, user_id, squat, alerts
):
    """Регрессия: раньше любая тренировка в этот день (пусть даже совсем другое
    упражнение — ручная запись жима) заставляла молча пропустить весь день из
    файла, хотя ни один подход в нём реально не задваивался."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим", gid)
    started_at = "2026-05-04T09:00:00"
    workout_id = await db.create_finished_workout(user_id, started_at, started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 1, 0, 60.0, 10, None)

    await csv_import.import_save(_callback(user_id), await _state(user_id, TWO_DAYS[:1], squat))

    assert await _totals(db, user_id) == (2, 3)
    assert "пропущено" not in alerts[-1]
