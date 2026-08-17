"""Оба входа в чат тренера должны давать один и тот же экран.

Входов два: инлайн-кнопка «🤖 AI-тренер» (callback menu:ai) и нижняя
reply-клавиатура / команда /ai_trainer. Готовые вопросы передавал только
инлайновый — а текст интро при этом в обоих случаях обещает «начни с готового
вопроса на кнопках ниже». Через нижнюю кнопку человек видел это обещание и одну
кнопку «Меню» под ним: экран врал сам себе, и зависело это от того, каким входом
человек вошёл.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import ai_trainer as ai_handler
from handlers import persistent_menu

pytestmark = pytest.mark.asyncio


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _message(user_id: int):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="u", language_code=None)
    message.answer = AsyncMock()
    return message


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(persistent_menu.ai_trainer, "is_configured", lambda: True)


async def test_bottom_button_shows_the_same_presets_as_inline(fresh_db, user_id):
    message = _message(user_id)
    await persistent_menu._open_ai_trainer(message, _state(user_id))

    shown = _labels(message.answer.await_args.kwargs["reply_markup"])
    for label, _cb in await ai_handler.intro_presets(user_id):
        assert label in shown, f"нижняя кнопка не показала готовый вопрос: {label}"


async def test_bottom_button_keeps_promise_of_the_intro_text(fresh_db, user_id):
    """Интро обещает кнопки — значит на этом же экране они обязаны быть."""
    message = _message(user_id)
    await persistent_menu._open_ai_trainer(message, _state(user_id))

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "на кнопках ниже" in text
    # Кроме «Меню» под текстом должно быть что-то ещё, иначе обещание пустое.
    assert len(_labels(markup)) > 1


async def test_returning_to_a_resumed_conversation_still_has_presets(fresh_db, user_id):
    """Заход через меню — отдельный экран входа, а не вклинивание в чужой ответ:
    пресеты стоят на нём независимо от того, шёл ли уже разговор."""
    state = _state(user_id)
    await state.update_data(ai_history=[{"role": "user", "content": "хай"}])

    message = _message(user_id)
    await persistent_menu._open_ai_trainer(message, state)

    shown = _labels(message.answer.await_args.kwargs["reply_markup"])
    for label, _cb in await ai_handler.intro_presets(user_id):
        assert label in shown
