"""Sanity ceilings on imported sets.

An impossible set that gets in through an import is silently permanent: it
becomes the exercise's all-time record, joins lifetime tonnage, and unlocks
weight-club achievements that are never revoked. The typed-set parser has
guarded against that for a while; the CSV path did not.
"""

import pytest

from handlers.csv_import import _build_workout_groups
from parser import ParseError

MAPPING = {"date": 0, "exercise": 1, "weight": 2, "reps": 3}


def _rows(weight: str, reps: str):
    return [["2026-05-04", "Жим лёжа", weight, reps]]


def test_a_plausible_row_still_imports():
    groups = _build_workout_groups(_rows("100", "8"), MAPPING)
    assert len(groups) == 1


@pytest.mark.parametrize(
    "weight, reps, expected",
    [
        ("99999", "8", "слишком большой вес"),
        ("-50", "8", "отрицательный вес"),
        ("100", "9999", "слишком много повторов"),
    ],
)
def test_impossible_rows_are_rejected_with_their_line_number(weight, reps, expected):
    with pytest.raises(ParseError) as excinfo:
        _build_workout_groups(_rows(weight, reps), MAPPING)
    assert expected in excinfo.value.message
    assert "Строка 2" in excinfo.value.message


@pytest.mark.asyncio
async def test_import_awards_achievements_for_the_imported_history(fresh_db, user_id, monkeypatch):
    """Importing a year of history can complete streaks, weight clubs and
    tonnage badges at once. Without a resync the grid stayed empty until the
    next live workout happened to trigger an evaluation."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    import handlers.csv_import as csv_import
    from fsm import ImportFlow

    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", gid)

    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(ImportFlow.confirming)
    await state.update_data(
        imp_workouts=[
            {"date": "2026-05-04", "entries": [{"name": "Присед", "sets": [(150.0, 5, None)]}]}
        ],
        imp_resolved={"Присед": ex_id},
    )

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
    callback.answer = AsyncMock()
    callback.message = message

    async def fake_show_settings(event, state, alert=None):
        return None

    monkeypatch.setattr("handlers.settings.show_settings", fake_show_settings)

    await csv_import.import_save(callback, state)

    codes = await db.list_achievement_codes(user_id)
    assert "first" in codes          # first workout ever
    assert "club100" in codes        # 150кг squat clears the 100кг club
