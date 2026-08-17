"""Поиск по истории работал молча: список умел фильтроваться по названию
упражнения, стоило начать печатать, но нигде не было сказано, что так можно —
человек должен был догадаться сам. Шапка экрана истории обязана звать к
поиску, а пустая выдача поиска — не быть тупиком."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import formatting
from handlers import history

pytestmark = pytest.mark.asyncio


def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _make_callback(user_id: int):
    message = MagicMock()
    message.answer = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    return msg


async def test_history_list_header_invites_search():
    text = formatting.build_history_list([])
    # Пустая история — про поиск говорить рано, там просто нечего искать.
    assert text == "Пока нет завершённых тренировок."


async def test_history_list_with_entries_announces_search_up_top():
    import datetime as dt

    entries = [(dt.datetime(2026, 7, 26, 13), ["Жим лёжа"], 3)]
    text = formatting.build_history_list(entries)
    lines = text.splitlines()
    # Приглашение к поиску — сразу под заголовком, а не в самом низу списка.
    assert "Ищешь конкретное упражнение" in lines[1]
    assert "напиши его название" in lines[1]


async def test_history_screen_shows_search_hint(fresh_db, user_id, monkeypatch):
    db = fresh_db
    await db.create_finished_workout(user_id, "2026-01-01T12:00:00", "2026-01-01T13:00:00")

    monkeypatch.setattr(history.ui, "safe_edit", AsyncMock())
    callback = _make_callback(user_id)

    await history.show_history_list(callback, _state(user_id), page=0)

    text = history.ui.safe_edit.await_args.args[1]
    assert "Ищешь конкретное упражнение" in text


async def test_search_with_no_matches_suggests_a_next_step(fresh_db, user_id):
    message = _make_message(user_id, "приседания")

    await history.hist_search(message, state=_state(user_id))

    text = message.answer.await_args.args[0]
    assert "Ничего не нашёл" in text
    assert "напиши другое упражнение" in text
