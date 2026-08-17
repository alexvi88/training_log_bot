"""Первый экран новичка — явный выбор языка ДО приветствия _ONBOARDING (см.
handlers/workout.py, cmd_start / onboarding_language_set и keyboards.py,
onboarding_language_keyboard). Подход к мокам — тот же, что и в
tests/test_i18n_middleware.py и tests/test_settings_language.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import i18n
from handlers import workout

pytestmark = pytest.mark.asyncio


def _make_message(user_id: int, language_code: str | None = None):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=language_code)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id)))
    message.delete = AsyncMock()
    return message


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "some previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2, chat=SimpleNamespace(id=user_id)))
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _reset_i18n():
    # Как в test_i18n_middleware.py: контекстная переменная общая на процесс,
    # порядок тестов не должен через неё протекать.
    token = i18n.current_lang.set(i18n.DEFAULT_LANG)
    yield
    i18n.current_lang.reset(token)


# --- новичок видит экран выбора языка, догадка помечена -----------------------


async def test_new_user_english_client_sees_screen_with_english_marked(fresh_db):
    message = _make_message(555001, language_code="en")
    state = await _make_state(555001)

    await workout.cmd_start(message, state)

    # Два сообщения: молчаливый носитель постоянной клавиатуры
    # (attach_silently — его нельзя откладывать до тапа, см. cmd_start) и
    # ПОСЛЕДНИМ экран выбора языка. Приветствие ещё не показано.
    assert message.answer.await_count == 2
    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert text == i18n.t_in("en", "screen.onboarding_language.title")
    labels = _labels(markup)
    assert "• English •" in labels
    assert "Русский" in labels


async def test_new_user_russian_client_sees_screen_with_russian_marked(fresh_db):
    message = _make_message(555002, language_code="ru")
    state = await _make_state(555002)

    await workout.cmd_start(message, state)

    markup = message.answer.await_args.kwargs["reply_markup"]
    labels = _labels(markup)
    assert "• Русский •" in labels
    assert "English" in labels


async def test_new_user_unknown_client_language_defaults_to_english_marked(fresh_db):
    """i18n.normalize сводит незнакомый code к английскому (см. i18n.py) — тот
    же дефолт должен быть помечен на экране выбора."""
    message = _make_message(555003, language_code="de")
    state = await _make_state(555003)

    await workout.cmd_start(message, state)

    markup = message.answer.await_args.kwargs["reply_markup"]
    labels = _labels(markup)
    assert "• English •" in labels


# --- выбор языка: пишется в базу, дальше приходит приветствие -----------------


async def test_choosing_language_writes_db_and_shows_onboarding(fresh_db):
    user_id = 555004
    message = _make_message(user_id, language_code="ru")
    state = await _make_state(user_id)
    await workout.cmd_start(message, state)  # заводит новичка, показывает экран выбора

    callback = _make_callback(user_id, "onboarding:lang:en")
    await workout.onboarding_language_set(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["lang"] == "en"
    assert i18n.get_lang() == "en"
    callback.answer.assert_awaited_once()
    # Экран выбора языка либо правится на месте, либо (раз клавиатура
    # attach_silently увела его со дна чата) присылается новым сообщением —
    # оба пути кончаются одним и тем же текстом приветствия.
    edited = callback.message.edit_text.await_args
    answered = callback.message.answer.await_args_list
    onboarding_text = edited.args[0] if edited is not None else answered[-1].args[0]
    assert "ПРИВЕТ АТЛЕТ" in onboarding_text


async def test_unknown_language_code_in_callback_is_ignored(fresh_db):
    user_id = 555005
    message = _make_message(user_id, language_code="ru")
    state = await _make_state(user_id)
    await workout.cmd_start(message, state)

    callback = _make_callback(user_id, "onboarding:lang:de")
    await workout.onboarding_language_set(callback, state)

    user = await fresh_db.get_user(user_id)
    # Незнакомый код (старая клавиатура/чужой клиент) не трогает базу и не роняет хендлер.
    assert user["lang"] == "ru"
    callback.answer.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()


# --- старожил экран выбора не видит никогда -----------------------------------


async def test_existing_user_start_does_not_see_language_screen(fresh_db, user_id):
    message = _make_message(user_id, language_code="en")
    state = await _make_state(user_id)

    await workout.cmd_start(message, state)

    # Единственное сообщение — главное меню/онбординг, а не экран выбора языка.
    text = message.answer.await_args.args[0]
    assert text != i18n.t_in("en", "screen.onboarding_language.title")


async def test_second_start_of_new_user_after_choice_skips_screen(fresh_db):
    """Новичок, уже выбравший язык, при повторном /start сразу видит обычное
    меню — экран выбора не всплывает второй раз."""
    user_id = 555006
    message = _make_message(user_id, language_code="ru")
    state = await _make_state(user_id)
    await workout.cmd_start(message, state)  # первый /start — заводит, показывает выбор

    callback = _make_callback(user_id, "onboarding:lang:en")
    await workout.onboarding_language_set(callback, state)  # выбор сделан

    message2 = _make_message(user_id, language_code="ru")
    state2 = await _make_state(user_id)
    await workout.cmd_start(message2, state2)  # второй /start

    text = message2.answer.await_args.args[0]
    assert text != i18n.t_in("en", "screen.onboarding_language.title")
    assert text != i18n.t_in("ru", "screen.onboarding_language.title")


async def test_menu_button_does_not_show_language_screen(fresh_db, user_id):
    """Кнопка «🏠 Меню» зовёт тот же cmd_start без command — старожил экран
    выбора не видит и тут (см. докстринг cmd_start)."""
    message = _make_message(user_id, language_code="en")
    state = await _make_state(user_id)

    await workout.cmd_start(message, state, command=None)

    text = message.answer.await_args.args[0]
    assert text != i18n.t_in("en", "screen.onboarding_language.title")
