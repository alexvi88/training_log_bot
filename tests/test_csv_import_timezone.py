"""Импорт CSV на верхней границе пикера часовых поясов (UTC+12).

`import_save` писал время импортируемой тренировки как константный
«безопасный полдень» ("{date}T12:00:00") прямо в UTC-колонку, не учитывая
tz_offset пользователя. Местный день восстанавливается как
`date(started_at, '+tz_offset hours')` (db._local_day) — и на UTC+12,
легальной верхней границе диапазона пикера (keyboards.py:1183,
`range(-1, 13)`), 12:00 + 12 часов даёт ровно полночь следующих суток.

Тренировка из файла на 15.03.2026 у пользователя на UTC+12 оказывалась
16.03.2026 в истории/на дашборде/в стриках — на день позже, чем показывал
экран подтверждения. Дубль-защита (`_duplicate_dates`) сравнивает даты из
файла с `db.list_finished_workout_dates` (которая возвращает уже сдвинутый
день), так что расхождение в сутки ломало и её: повторная загрузка того же
файла не распознавалась как дубль.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.csv_import as csv_import
from fsm import ImportFlow

ONE_DAY = [
    {"date": "2026-03-15", "entries": [{"name": "Присед", "sets": [(100.0, 5, None)]}]},
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


@pytest.fixture
def alerts(monkeypatch):
    seen: list[str] = []

    async def fake_show_settings(event, state, alert=None):
        seen.append(alert)

    monkeypatch.setattr("handlers.settings.show_settings", fake_show_settings)
    return seen


@pytest.fixture
async def squat(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    return await fresh_db.create_exercise(user_id, "Присед", gid)


@pytest.mark.parametrize("tz_offset", [-11, -5, -1, 0, 5, 12, 14])
async def test_imported_date_survives_every_tz_offset_in_the_picker_range(
    fresh_db, user_id, squat, alerts, tz_offset
):
    """Импортированная дата должна остаться той же датой у любого офсета из
    диапазона пикера (−11…+14), а не только у большинства из них."""
    await fresh_db.update_user(user_id, tz_offset=tz_offset)

    await csv_import.import_save(_callback(user_id), await _state(user_id, ONE_DAY, squat))

    dates = await fresh_db.list_finished_workout_dates(user_id, tz_offset=tz_offset)
    assert dates == ["2026-03-15"]


async def test_reimporting_the_same_file_at_utc_plus_12_is_recognized_as_a_duplicate(
    fresh_db, user_id, squat, alerts
):
    """До фикса UTC+12 сдвигал сохранённую дату на день вперёд, из-за чего
    `_duplicate_dates` не находила совпадения и повторный импорт того же
    файла молча удваивал историю."""
    await fresh_db.update_user(user_id, tz_offset=12)

    await csv_import.import_save(_callback(user_id), await _state(user_id, ONE_DAY, squat))
    assert await fresh_db.count_workouts(user_id) == 1

    await csv_import.import_save(_callback(user_id), await _state(user_id, ONE_DAY, squat))

    assert await fresh_db.count_workouts(user_id) == 1
    assert "уже есть" in alerts[-1]
