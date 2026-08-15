"""Карточка бота (description/short_description): текст, лимиты Telegram и
экономный вызов API — выставляем только когда текущее значение разошлось."""
import re
from unittest.mock import AsyncMock

from aiogram.types import BotDescription, BotShortDescription

import bot_profile


def test_description_opens_with_the_brand_formula():
    """Формула по правилу PR #462: «ПРИВЕТ АТЛЕТ!» — без запятой после АТЛЕТ."""
    assert bot_profile.DESCRIPTION.startswith("ПРИВЕТ АТЛЕТ! ")
    assert "ПРИВЕТ АТЛЕТ," not in bot_profile.DESCRIPTION


def test_description_and_short_description_fit_telegram_limits():
    assert len(bot_profile.DESCRIPTION) <= 512
    assert len(bot_profile.SHORT_DESCRIPTION) <= 120


def test_description_does_not_repeat_nachnyom():
    """«Начнём» — раньше встречалось дважды (заголовок + дубль в теле)."""
    assert len(re.findall(r"(?i)начнём", bot_profile.DESCRIPTION)) <= 1


def test_description_never_drops_the_yo():
    """«НАЧНЕМ» без «ё» — старая опечатка, которую этот текст как раз чинит."""
    assert "НАЧНЕМ" not in bot_profile.DESCRIPTION
    assert not re.search(r"(?i)начнем(?!ё)", bot_profile.DESCRIPTION)


async def test_sync_skips_api_calls_when_already_up_to_date():
    bot = AsyncMock()
    bot.get_my_description.return_value = BotDescription(description=bot_profile.DESCRIPTION)
    bot.get_my_short_description.return_value = BotShortDescription(
        short_description=bot_profile.SHORT_DESCRIPTION
    )

    await bot_profile.sync_bot_profile(bot)

    bot.set_my_description.assert_not_called()
    bot.set_my_short_description.assert_not_called()


async def test_sync_sets_both_when_stale():
    bot = AsyncMock()
    bot.get_my_description.return_value = BotDescription(description="устарело")
    bot.get_my_short_description.return_value = BotShortDescription(short_description="устарело")

    await bot_profile.sync_bot_profile(bot)

    bot.set_my_description.assert_awaited_once_with(bot_profile.DESCRIPTION)
    bot.set_my_short_description.assert_awaited_once_with(bot_profile.SHORT_DESCRIPTION)
