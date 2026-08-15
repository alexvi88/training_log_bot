"""Карточка бота в Telegram — «What can this bot do?» и краткое описание в
поиске/списке чатов. Раньше эти тексты жили только в BotFather и правились
руками, так что отставали от гайда (TONE_OF_VOICE.md) при каждой правке
формулы «ПРИВЕТ АТЛЕТ» — теперь текст в репозитории и выставляется кодом.

Лимиты Telegram: description — 512 символов, short_description — 120.
"""

import logging

from aiogram import Bot

logger = logging.getLogger(__name__)

# Показывается на пустом экране чата с ботом (BotFather: /setdescription).
# Формула бренда — по правилу PR #462: «ПРИВЕТ АТЛЕТ!», без запятой после АТЛЕТ.
DESCRIPTION = (
    "ПРИВЕТ АТЛЕТ! НАЧНЁМ ТРЕНИРОВКУ?\n\n"
    "Дневник тренировок, рекорды, программа, разбор твоей истории — всё на мне.\n"
    "Железо — на тебе."
)

# Показывается в поиске и в списке чатов рядом с именем бота (BotFather:
# /setabouttext). Короче description — тут не до формулы приветствия, только
# суть.
SHORT_DESCRIPTION = "Дневник тренировок с AI-тренером: подходы, рекорды, программы. Веду сам — тренируешься ты."

_DESCRIPTION_LIMIT = 512
_SHORT_DESCRIPTION_LIMIT = 120

assert len(DESCRIPTION) <= _DESCRIPTION_LIMIT, "description превышает лимит Telegram"
assert len(SHORT_DESCRIPTION) <= _SHORT_DESCRIPTION_LIMIT, "short_description превышает лимит Telegram"


async def sync_bot_profile(bot: Bot) -> None:
    """Выставляет карточку бота, только если она разошлась с тем, что уже
    стоит в Telegram — иначе каждый рестарт дёргал бы оба метода вхолостую."""
    current = await bot.get_my_description()
    if current.description != DESCRIPTION:
        await bot.set_my_description(DESCRIPTION)
        logger.info("Обновил описание бота (get_my_description/set_my_description)")

    current_short = await bot.get_my_short_description()
    if current_short.short_description != SHORT_DESCRIPTION:
        await bot.set_my_short_description(SHORT_DESCRIPTION)
        logger.info("Обновил краткое описание бота (set_my_short_description)")
