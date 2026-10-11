"""«🆕 Новый разговор» у AI-тренера в боте.

Кнопка стоит только на экране входа в уже идущий разговор (оба входа —
menu:ai и нижняя кнопка), а не под каждым ответом. Тап обнуляет то, что
уходит модели (ai_history), и всё, что висело на старом разговоре
(черновик программы, опросник, откаты), — так же, как DELETE /v1/ai/history в
приложении. Постоянный дневник переписки (ai_chat_messages) не трогается.
Модель на этом шаге не зовётся.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import ai_trainer
import i18n
from handlers import ai_trainer as ai_handler
from handlers import persistent_menu

pytestmark = pytest.mark.asyncio


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 10
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=11))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)


@pytest.fixture
def shown(monkeypatch):
    """Экран, который хендлер поставил через ui.safe_edit: (text, reply_markup)."""
    edit = AsyncMock()
    monkeypatch.setattr(ai_handler.ui, "safe_edit", edit)

    def last():
        call = edit.await_args
        return call.args[1], call.kwargs["reply_markup"]

    return last


async def _going_conversation(user_id: int) -> FSMContext:
    state = _state(user_id)
    await state.update_data(
        ai_history=[
            {"role": "user", "content": "как мой жим?"},
            {"role": "assistant", "content": "растёт"},
        ],
        ai_program_draft={"id": "abcd1234", "name": "Верх/низ", "days": []},
        ai_setup={"questions": [], "idx": 0},
        ai_actions=[{"label": "x", "callback": "ai:undo:u1"}],
        ai_undo={"u1": {"kind": "noop"}},
        ai_undo_seq=1,
        # Чужое для разговора — переживает сброс.
        workout_id=77,
    )
    return state


async def test_fresh_entry_has_no_new_chat_button(fresh_db, user_id, shown):
    callback = _callback(user_id, "menu:ai")
    await ai_handler.menu_ai(callback, _state(user_id))
    _, markup = shown()
    assert "ai:newchat" not in _callbacks(markup)


async def test_resume_entry_has_new_chat_button_on_both_entries(fresh_db, user_id, shown):
    callback = _callback(user_id, "menu:ai")
    await ai_handler.menu_ai(callback, await _going_conversation(user_id))
    _, markup = shown()
    assert "ai:newchat" in _callbacks(markup)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="u", language_code=None)
    message.answer = AsyncMock()
    await persistent_menu._open_ai_trainer(message, await _going_conversation(user_id))
    assert "ai:newchat" in _callbacks(message.answer.await_args.kwargs["reply_markup"])


async def test_answer_keyboard_does_not_carry_the_button(fresh_db, user_id):
    """Под ответом тренера кнопки нет — чат не засоряется."""
    markup = await ai_handler.ai_keyboard(user_id, answer="Жми дальше")
    assert "ai:newchat" not in _callbacks(markup)


async def test_new_chat_clears_model_context_but_keeps_the_log(fresh_db, user_id, monkeypatch, shown):
    db = fresh_db
    await db.add_ai_chat_message(user_id, "user", "как мой жим?")
    await db.add_ai_chat_message(user_id, "assistant", "растёт")
    ask = AsyncMock()
    monkeypatch.setattr(ai_trainer, "ask", ask)

    state = await _going_conversation(user_id)
    callback = _callback(user_id, "ai:newchat")
    await ai_handler.ai_new_chat(callback, state)

    data = await state.get_data()
    assert not data.get("ai_history")
    for key in ("ai_program_draft", "ai_setup", "ai_actions", "ai_undo"):
        assert not data.get(key), key
    assert data.get("workout_id") == 77
    assert await state.get_state() == ai_handler.AITrainerFlow.chatting

    # Старый разговор в дневнике переписки цел.
    log = await db.get_ai_chat_history(user_id)
    assert [row["content"] for row in log][-2:] in (["как мой жим?", "растёт"], ["растёт", "как мой жим?"])

    ask.assert_not_awaited()
    text, markup = shown()
    assert text == i18n.t("ai.screen.new_chat")
    # Сам экран нового разговора — уже свежий: кнопки «новый разговор» на нём нет,
    # готовые вопросы есть.
    assert "ai:newchat" not in _callbacks(markup)
    assert any(cb.startswith("ai:preset:") for cb in _callbacks(markup))


async def test_entering_again_after_new_chat_shows_intro(fresh_db, user_id, shown):
    state = await _going_conversation(user_id)
    await ai_handler.ai_new_chat(_callback(user_id, "ai:newchat"), state)
    await ai_handler.menu_ai(_callback(user_id, "menu:ai"), state)
    text, markup = shown()
    assert text.startswith(i18n.t("ai.screen.intro"))
    assert "ai:newchat" not in _callbacks(markup)


async def test_new_chat_refused_while_answer_in_flight(fresh_db, user_id):
    state = await _going_conversation(user_id)
    callback = _callback(user_id, "ai:newchat")
    ai_handler._busy.add(user_id)
    try:
        await ai_handler.ai_new_chat(callback, state)
    finally:
        ai_handler._busy.discard(user_id)
    assert (await state.get_data()).get("ai_history")
    assert callback.answer.await_args.kwargs.get("show_alert") is True
