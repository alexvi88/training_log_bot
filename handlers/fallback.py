"""Catch-all for text that doesn't match any state-specific handler.

Registered last in main.py so every other router gets first refusal; only text
typed with no active flow (or in a state with no dedicated text handler, e.g.
main menu, group pickers) ends up here instead of being silently dropped.
"""

from aiogram import Router
from aiogram.types import Message

router = Router(name="fallback")


@router.message()
async def unhandled_text(message: Message) -> None:
    await message.reply("Не понял 🤔 Нажми /start, чтобы вернуться в меню.")
