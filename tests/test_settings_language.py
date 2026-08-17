"""Экран выбора языка интерфейса в настройках: кнопка, клавиатура, запись в базу
и переключение контекста i18n (см. handlers/settings.py, keyboards.py)."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import i18n
import keyboards
from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


@pytest.fixture(autouse=True)
def _reset_lang_context():
    """i18n.current_lang — контекстная переменная, но тесты в одном процессе
    могут делить event loop, поэтому сбрасываем явно, чтобы порядок тестов не
    протекал через global-контекст."""
    i18n.set_lang(i18n.DEFAULT_LANG)
    yield
    i18n.set_lang(i18n.DEFAULT_LANG)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_settings_keyboard_shows_language_button_with_current_choice():
    kb = keyboards.settings_keyboard(
        "kg", "epley", True, True, True, lang="ru",
    )
    assert any(label == "🌐 Язык: Русский" for label in _labels(kb))

    kb_en = keyboards.settings_keyboard(
        "kg", "epley", True, True, True, lang="en",
    )
    assert any(label == "🌐 Language: English" for label in _labels(kb_en))


async def test_language_button_label_has_no_cyrillic_for_english_user():
    """Экран настроек вокруг пока русский — но подпись кнопки языка обязана
    читаться по-английски: это единственная дорога наружу для человека,
    попавшего не на свой язык, и оставлять её русской значит запереть его.

    Автоним «Русский» в подписи ЧУЖОГО языка тут появиться не может: подпись
    всегда показывает текущий выбор, то есть English.
    """
    kb_en = keyboards.settings_keyboard(
        "kg", "epley", True, True, True, lang="en",
    )
    lang_labels = [label for label in _labels(kb_en) if "🌐" in label]
    assert lang_labels, "кнопка языка пропала из настроек"
    for label in lang_labels:
        assert not re.search("[А-Яа-яЁё]", label), f"русский текст в английской подписи: {label!r}"


async def test_settings_lang_opens_screen_with_both_languages(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:lang")

    await settings.settings_language(callback, state)

    markup = callback.message.answer.call_args.kwargs["reply_markup"]
    labels = _labels(markup)
    assert any("Русский" in label for label in labels)
    assert any("English" in label for label in labels)
    callback.answer.assert_awaited_once()


async def test_language_keyboard_marks_current_language():
    kb_ru = keyboards.language_keyboard("ru")
    labels_ru = _labels(kb_ru)
    assert "• Русский •" in labels_ru
    assert "English" in labels_ru

    kb_en = keyboards.language_keyboard("en")
    labels_en = _labels(kb_en)
    assert "• English •" in labels_en
    assert "Русский" in labels_en


async def test_settings_langset_en_writes_db_and_switches_context(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    assert (await db.get_user(user_id))["lang"] == "ru"

    callback = _make_callback(user_id, "settings:langset:en")
    await settings.settings_language_set(callback, state)

    assert (await db.get_user(user_id))["lang"] == "en"
    # Порядок важен: к моменту перерисовки экрана настроек контекст уже
    # переключён — иначе выбор English рисовал бы русский экран.
    assert i18n.get_lang() == "en"


async def test_settings_langset_unknown_code_does_not_crash_or_touch_db(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "settings:langset:de")
    await settings.settings_language_set(callback, state)

    assert (await db.get_user(user_id))["lang"] == "ru"
    callback.answer.assert_awaited_once()


async def test_language_catalog_keys_present_in_both_locales():
    """Полнота своих ключей: всё, что добавлено под screen.language.*, должно
    быть в обоих каталогах — иначе англоязычный человек не сможет прочитать
    экран выбора языка (см. задачу)."""
    added_keys = {"screen.language.title", "screen.language.set_alert"}
    ru_catalog = i18n._load_catalog("ru")
    en_catalog = i18n._load_catalog("en")
    for key in added_keys:
        assert key in ru_catalog, f"{key} отсутствует в locales/ru.json"
        assert key in en_catalog, f"{key} отсутствует в locales/en.json"


async def test_switching_language_resends_the_bottom_keyboard(fresh_db, user_id):
    """Нижняя клавиатура прикрепляется один раз и живёт в чате с теми
    подписями, что были при отправке. Экраны перерисовываются, а она — нет, и
    человек оставался с «Workout / Menu / AI Coach» под русским ботом.

    Нажатия при этом работали (BTN_* сравнивают себя со всеми языками сразу),
    поэтому баг был чисто визуальным — и оттого невидимым для тестов, которые
    проверяли только то, что кнопка срабатывает.
    """
    from aiogram.types import ReplyKeyboardMarkup

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:langset:en")

    await settings.settings_language_set(callback, state)

    keyboard_calls = [
        call for call in callback.message.answer.await_args_list
        if isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardMarkup)
    ]
    assert keyboard_calls, "клавиатура не перевыслана — подписи внизу останутся на прежнем языке"
    labels = [b.text for row in keyboard_calls[-1].kwargs["reply_markup"].keyboard for b in row]
    assert "Workout" in labels, labels
    assert not any(re.search("[А-Яа-яЁё]", label) for label in labels), labels
