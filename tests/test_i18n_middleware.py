"""SetUserLanguageMiddleware (main.py) — язык рендера выставляется до хендлера,
а на первом /start (handlers/workout.py, cmd_start) язык ещё и угадывается и
закрепляется в базе.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

import db
import i18n
from main import SetUserLanguageMiddleware

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_i18n():
    # Как в tests/test_i18n.py: каждый тест должен видеть дефолтный язык на
    # входе, иначе порядок запуска тестов начнёт влиять на результат.
    token = i18n.current_lang.set(i18n.DEFAULT_LANG)
    yield
    i18n.current_lang.reset(token)


def _make_message(user_id: int, language_code: str | None = None):
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=language_code)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=user_id)))
    message.delete = AsyncMock()
    return message


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


# --- middleware сама по себе --------------------------------------------------


async def test_middleware_sets_lang_from_db_for_existing_user(fresh_db, user_id):
    await fresh_db.set_user_lang(user_id, "en")
    message = _make_message(user_id)
    handler = AsyncMock(return_value="handled")
    middleware = SetUserLanguageMiddleware()

    result = await middleware(handler, message, {})

    assert result == "handled"
    handler.assert_awaited_once_with(message, {})
    assert i18n.get_lang() == "en"


async def test_middleware_guesses_from_language_code_for_unknown_user(fresh_db):
    message = _make_message(999999, language_code="uk")
    handler = AsyncMock(return_value="handled")
    middleware = SetUserLanguageMiddleware()

    await middleware(handler, message, {})

    assert i18n.get_lang() == "ru"
    # Догадка живёт только в контексте — в базу для незаведённого пользователя
    # ничего не пишется, записи как не было, так и нет.
    assert await fresh_db.get_user(999999) is None


async def test_middleware_survives_db_failure(fresh_db, user_id, monkeypatch):
    async def _boom(_telegram_id):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(db, "get_user", _boom)
    message = _make_message(user_id)
    handler = AsyncMock(return_value="handled")
    middleware = SetUserLanguageMiddleware()

    result = await middleware(handler, message, {})

    assert result == "handled"
    handler.assert_awaited_once_with(message, {})
    assert i18n.get_lang() == i18n.DEFAULT_LANG


@pytest.mark.parametrize(
    "language_code, expected",
    [
        ("de", "en"),  # неподдерживаемый язык клиента -> английский
        ("uk", "ru"),  # язык СНГ-пространства -> русский
    ],
)
async def test_middleware_normalizes_unknown_client_languages(fresh_db, language_code, expected):
    message = _make_message(888888, language_code=language_code)
    handler = AsyncMock(return_value="handled")
    middleware = SetUserLanguageMiddleware()

    await middleware(handler, message, {})

    assert i18n.get_lang() == expected


# --- догадка на первом /start (handlers/workout.py) ---------------------------


async def test_first_start_stores_guessed_language(fresh_db):
    from handlers import workout

    message = _make_message(555555, language_code="en")
    state = await _make_state(555555)

    await workout.cmd_start(message, state)

    user = await fresh_db.get_user(555555)
    assert user["lang"] == "en"


async def test_second_start_does_not_overwrite_chosen_language(fresh_db, user_id):
    from handlers import workout

    message = _make_message(user_id, language_code="en")
    state = await _make_state(user_id)

    # Первый /start — заводит пользователя (fresh_db.get_or_create_user в
    # фикстуре user_id уже сделал это без языка), тут же руками имитируем, что
    # атлет позже осознанно выбрал язык в settings.py.
    await fresh_db.set_user_lang(user_id, "ru")

    await workout.cmd_start(message, state)

    user = await fresh_db.get_user(user_id)
    assert user["lang"] == "ru"
