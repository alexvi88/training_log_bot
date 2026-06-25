"""Shared helper for keeping bot screens at the bottom of the chat."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


async def safe_edit(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None) -> Message:
    """Replace the callback's message with a fresh one instead of editing in place.

    Telegram can't move an edited message down past newer ones, so if other
    messages landed in the chat after this one was sent, an in-place edit
    would leave a stale screen stuck above them. Deleting and resending
    keeps every menu screen at the bottom, right where the user just tapped.
    """
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    return await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
