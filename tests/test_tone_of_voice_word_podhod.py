"""TONE_OF_VOICE.md запрещает слово «сет» в прозе (словарь: «Говорим:
подход | Не говорим: сет») — оно всё ещё встречалось в трёх местах:

- подвал карточки тренировки (`formatting.build_workout_card`), который
  видит каждый пользователь на каждой законченной тренировке и который
  уходит в PNG-картинку шеринга (`charts.render_workout_card`);
- подтверждение удаления тренировки из истории (`handlers/history.py`,
  `_delete_confirm_text`);
- творительный падеж в подтверждении «Убрать … вместе с N сетом/сетами»
  (`handlers/edit_workout.py`) — находка 24 уже чинила падеж, но оставила
  запрещённый корень «сет».
"""

import datetime as dt

import db
import formatting
from formatting import ExerciseBlockView
from handlers import history


def test_workout_card_footer_says_podhod_not_set():
    block = ExerciseBlockView(
        group_name="Грудь", exercise_name="Жим штанги лёжа", sets=[(100.0, 8), (100.0, 8)],
    )
    _title, _body, footer, _note = formatting.build_workout_card(
        dt.datetime(2026, 8, 6, 10, 0), [block]
    )

    assert "подход" in footer
    assert "сет" not in footer


async def test_delete_confirm_text_says_podhod_not_set(fresh_db, user_id):
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    ex_id = await fresh_db.create_exercise(user_id, "Присед", gid)
    workout_id = await fresh_db.create_finished_workout(
        user_id, "2026-08-06T10:00:00", "2026-08-06T10:05:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8, None)
    workout = await db.get_workout(workout_id)

    text = await history._delete_confirm_text(workout)

    assert "подход" in text
    assert "сет" not in text


async def test_remove_exercise_confirm_uses_instrumental_podhodom(fresh_db, user_id, monkeypatch):
    """«Убрать … вместе с N подходом/подходами» — не «сетом/сетами»."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import edit_workout

    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    ex_id = await fresh_db.create_exercise(user_id, "Присед", gid)
    workout_id = await fresh_db.create_workout(user_id)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8)

    ui_calls = []

    async def fake_safe_edit(callback, text, **kwargs):
        ui_calls.append(text)

    monkeypatch.setattr(edit_workout.ui, "safe_edit", fake_safe_edit)

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.data = f"editw:rmexask:{block_id}"
    callback.answer = AsyncMock()

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.update_data(edit_exercise_id=ex_id)

    await edit_workout.editw_remove_exercise_confirm(callback, state)

    assert "подходом" in ui_calls[0]
    assert "сет" not in ui_calls[0]
