"""Английская карточка бота и «/»-меню команд: Telegram выбирает эти тексты по
системному языку клиента, а не по нашей колонке users.lang, поэтому оба
набора (дефолтный — русский, и language_code="en") должны заливаться отдельно
и не пересекаться по алфавиту."""
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BotCommandScopeDefault

import bot_profile
import config
from main import _public_commands, _setup_commands

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def _load(lang: str) -> dict:
    with (_LOCALES_DIR / f"{lang}.json").open(encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_setup_commands_uploads_default_and_english_sets(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", None)
    bot = AsyncMock()

    await _setup_commands(bot)

    default_calls = [
        c for c in bot.set_my_commands.call_args_list if isinstance(c.kwargs.get("scope"), BotCommandScopeDefault)
    ]
    assert len(default_calls) == 2
    # Ru-набор заливается без language_code — это дефолт, который видят все
    # языки без своего варианта, а не "применить ко всем".
    ru_call = next(c for c in default_calls if c.kwargs.get("language_code") is None)
    en_call = next(c for c in default_calls if c.kwargs.get("language_code") == "en")
    assert [cmd.command for cmd in ru_call.args[0]] == [cmd.command for cmd in en_call.args[0]]


def test_english_commands_have_no_cyrillic():
    for cmd in _public_commands("en"):
        assert not _CYRILLIC_RE.search(cmd.description), cmd.description


def test_russian_commands_are_still_russian():
    # Смысловая проверка, что каталог не перепутан местами: русский набор
    # реально русский (кириллица есть хотя бы где-то в списке).
    joined = " ".join(cmd.description for cmd in _public_commands("ru"))
    assert _CYRILLIC_RE.search(joined)


@pytest.mark.asyncio
async def test_sync_bot_profile_sets_description_on_both_languages():
    bot = AsyncMock()
    bot.get_my_description.return_value.description = "устарело"
    bot.get_my_short_description.return_value.short_description = "устарело"

    await bot_profile.sync_bot_profile(bot)

    calls = bot.set_my_description.await_args_list
    languages = {c.kwargs.get("language_code") for c in calls}
    assert languages == {None, "en"}

    short_calls = bot.set_my_short_description.await_args_list
    short_languages = {c.kwargs.get("language_code") for c in short_calls}
    assert short_languages == {None, "en"}


def test_english_description_has_no_cyrillic():
    assert not _CYRILLIC_RE.search(bot_profile.DESCRIPTION_EN)
    assert not _CYRILLIC_RE.search(bot_profile.SHORT_DESCRIPTION_EN)


def test_description_lengths_fit_telegram_limits():
    """Лимиты Bot API: description — 512 символов, short_description — 120
    (setMyDescription / setMyShortDescription)."""
    for text in (bot_profile.DESCRIPTION, bot_profile.DESCRIPTION_EN):
        assert len(text) <= 512
    for text in (bot_profile.SHORT_DESCRIPTION, bot_profile.SHORT_DESCRIPTION_EN):
        assert len(text) <= 120


def test_english_description_has_no_please():
    """Тон-оф-войс: вежливый корпоративный слой ("please" и подобное)
    запрещён в английской локализации."""
    lowered = (bot_profile.DESCRIPTION_EN + " " + bot_profile.SHORT_DESCRIPTION_EN).lower()
    assert "please" not in lowered


@pytest.mark.parametrize(
    "key",
    [
        "bot.description",
        "bot.short_description",
        "bot.commands.start",
        "bot.commands.help",
        "bot.commands.ai_trainer",
        "bot.commands.food_diary",
        "bot.commands.feedback",
        "bot.commands.mcp",
        "bot.commands.game",
        "bot.commands.community",
    ],
)
def test_bot_keys_present_in_both_catalogs(key):
    ru = _load("ru")
    en = _load("en")
    assert key in ru, f"{key} отсутствует в locales/ru.json"
    assert key in en, f"{key} отсутствует в locales/en.json"
    assert ru[key].strip()
    assert en[key].strip()


def test_english_catalog_values_are_ascii_ish():
    """Английские bot.* строки не должны содержать кириллицу — иначе это
    забытый русский текст под английским ключом."""
    en = _load("en")
    for key, value in en.items():
        if key.startswith("bot."):
            assert not _CYRILLIC_RE.search(value), f"{key}: {value!r} содержит кириллицу"


def test_ru_and_en_bot_texts_are_actually_different():
    """Если тексты совпали дословно — кто-то скопипастил русский под
    английский ключ вместо перевода."""
    ru = _load("ru")
    en = _load("en")
    for key in ru:
        if key.startswith("bot.") and key in en:
            assert ru[key] != en[key], f"{key}: одинаковый текст в ru и en"

