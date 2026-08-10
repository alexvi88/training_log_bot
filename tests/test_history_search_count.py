"""Поиск по истории («жим» и т.п.) врал про число найденных тренировок,
когда их было больше 20.

`db.search_workouts_by_exercise` режет выдачу `LIMIT 20` по умолчанию, а
`handlers.history.hist_search` печатал в заголовке `len(entries)` — то есть
«сколько влезло под лимит», а не «сколько всего нашлось». Прямое нарушение
правила TONE_OF_VOICE «любое утверждение о данных пользователя отправляется
только когда данные его подтверждают».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import history


def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    return msg


async def _log_bench_workouts(db, user_id: int, count: int) -> int:
    gid = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим штанги лёжа", gid)
    for i in range(count):
        started = f"2026-01-{i + 1:02d}T12:00:00"
        workout_id = await db.create_finished_workout(user_id, started, started, source="live")
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        await db.add_set(block_id, ex_id, 1, 0, 100.0, 5, None)
    return ex_id


async def test_header_shows_the_real_total_when_more_than_20_match(fresh_db, user_id):
    db = fresh_db
    await _log_bench_workouts(db, user_id, 25)
    message = _make_message(user_id, "жим")

    await history.hist_search(message, state=_state(user_id))

    text = message.answer.await_args.args[0]
    assert "25" in text
    assert "20 из 25" in text  # список честно помечен как урезанный


async def test_header_shows_plain_count_when_everything_fits(fresh_db, user_id):
    db = fresh_db
    await _log_bench_workouts(db, user_id, 3)
    message = _make_message(user_id, "жим")

    await history.hist_search(message, state=_state(user_id))

    text = message.answer.await_args.args[0]
    assert "«жим»: 3" in text
    assert "из" not in text


def _make_callback(user_id: int, data: str):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    return callback


async def test_search_pagination_reaches_workouts_past_the_first_page(fresh_db, user_id, monkeypatch):
    """Регрессия: старые тренировки частого упражнения были физически
    недостижимы через поиск — has_next всегда был False, кнопки «Ещё» не было."""
    db = fresh_db
    await _log_bench_workouts(db, user_id, 25)
    message = _make_message(user_id, "жим")
    state = _state(user_id)

    await history.hist_search(message, state=state)
    first_kb = message.answer.await_args.kwargs["reply_markup"]
    first_page_buttons = [b.callback_data for row in first_kb.inline_keyboard for b in row]
    assert "hist:spage:1" in first_page_buttons

    edited = {}

    async def fake_safe_edit(callback, text, **kwargs):
        edited["text"] = text
        edited["reply_markup"] = kwargs.get("reply_markup")

    monkeypatch.setattr(history.ui, "safe_edit", fake_safe_edit)
    callback = _make_callback(user_id, "hist:spage:1")

    await history.hist_search_page(callback, state=state)

    assert "«жим»: 25" in edited["text"]  # вторая страница добирает остаток — счёт полный
    second_page_buttons = [
        b.callback_data for row in edited["reply_markup"].inline_keyboard for b in row
    ]
    assert "hist:spage:0" in second_page_buttons
    assert "hist:spage:2" not in second_page_buttons  # 25 тренировок влезают в 2 страницы по 20
