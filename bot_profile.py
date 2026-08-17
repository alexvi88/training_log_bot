"""Карточка бота в Telegram — «What can this bot do?» и краткое описание в
поиске/списке чатов. Раньше эти тексты жили только в BotFather и правились
руками, так что отставали от гайда (TONE_OF_VOICE.md) при каждой правке
формулы «ПРИВЕТ АТЛЕТ» — теперь текст в репозитории и выставляется кодом.

Telegram выбирает description/short_description и «/»-меню команд по
системному языку КЛИЕНТА пользователя, а не по нашей колонке users.lang — этот
механизм вообще не завязан на базу и мидлварь, тексты заливаются один раз при
старте бота, отдельно на каждый language_code. Поэтому оба набора (русский —
default, английский — language_code="en") заливаются отдельными вызовами:
вызов БЕЗ language_code — это дефолт, который видят все языки, для которых нет
своего варианта (не «применить ко всем», см. доки Bot API на set_my_description
/ set_my_commands), а с language_code="en" — вариант только для en.

Лимиты Telegram: description — 512 символов, short_description — 120.
"""

import logging

from aiogram import Bot

import i18n

logger = logging.getLogger(__name__)

# Показывается на пустом экране чата с ботом (BotFather: /setdescription).
# Формула бренда — по правилу PR #462: «ПРИВЕТ АТЛЕТ!», без запятой после АТЛЕТ.
# Тексты живут в каталоге (locales/*.json, ключи bot.*), а не литералами — их
# вычитывают вместе с остальными пользовательскими текстами.
DESCRIPTION = i18n.t_in("ru", "bot.description")
DESCRIPTION_EN = i18n.t_in("en", "bot.description")

# Показывается в поиске и в списке чатов рядом с именем бота (BotFather:
# /setabouttext). Короче description — тут не до формулы приветствия, только
# суть.
SHORT_DESCRIPTION = i18n.t_in("ru", "bot.short_description")
SHORT_DESCRIPTION_EN = i18n.t_in("en", "bot.short_description")

_DESCRIPTION_LIMIT = 512
_SHORT_DESCRIPTION_LIMIT = 120

for _text in (DESCRIPTION, DESCRIPTION_EN):
    assert len(_text) <= _DESCRIPTION_LIMIT, "description превышает лимит Telegram"
for _text in (SHORT_DESCRIPTION, SHORT_DESCRIPTION_EN):
    assert len(_text) <= _SHORT_DESCRIPTION_LIMIT, "short_description превышает лимит Telegram"


async def _sync_description(bot: Bot, *, language_code: str | None, text: str) -> None:
    current = await bot.get_my_description(language_code=language_code)
    if current.description != text:
        await bot.set_my_description(text, language_code=language_code)
        logger.info("Обновил описание бота (language_code=%r)", language_code)


async def _sync_short_description(bot: Bot, *, language_code: str | None, text: str) -> None:
    current = await bot.get_my_short_description(language_code=language_code)
    if current.short_description != text:
        await bot.set_my_short_description(text, language_code=language_code)
        logger.info("Обновил краткое описание бота (language_code=%r)", language_code)


async def sync_bot_profile(bot: Bot) -> None:
    """Выставляет карточку бота на обоих языках, только там, где она разошлась
    с тем, что уже стоит в Telegram — иначе каждый рестарт дёргал бы все
    методы вхолостую. language_code=None — дефолт (русский, видят все языки
    без своего варианта), "en" — отдельный вариант для англоязычных."""
    await _sync_description(bot, language_code=None, text=DESCRIPTION)
    await _sync_description(bot, language_code="en", text=DESCRIPTION_EN)
    await _sync_short_description(bot, language_code=None, text=SHORT_DESCRIPTION)
    await _sync_short_description(bot, language_code="en", text=SHORT_DESCRIPTION_EN)
