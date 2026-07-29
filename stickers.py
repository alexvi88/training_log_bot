"""Optional sticker reactions, drawn live from a public Telegram sticker pack.

The pack isn't ours and isn't vendored into the repo: nothing here ships a
single sticker file. The bot only knows a pack's *short name* (the bit after
t.me/addstickers/), asks Telegram for its contents at runtime via
getStickerSet, and re-sends stickers by the file_id Telegram hands back — the
same thing a user does when they forward one. Swap the pack by changing
STICKER_PACK_NAMES; no code or assets change with it.

Which sticker fits a moment is decided by the emoji the pack's author attached
to each sticker, not by any hardcoded index — an index would silently point at
the wrong picture the day the author reorders the pack. Every occasion below
lists the emoji it would like, best first, and falls back to "anything in the
pack" when none of them are present. So a gym pack, a cat pack and an empty
config all behave sensibly: the right sticker, some sticker, or no sticker.

Nothing here is ever allowed to break a flow. Every public function swallows
its own errors and answers "no sticker" — a pack that's been deleted, renamed
or is briefly unreachable costs the user nothing but the sticker itself.
"""

from __future__ import annotations

import logging
import random
import time
from collections import OrderedDict

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import config
import db

logger = logging.getLogger(__name__)

# Occasion keys. These name *moments*, not pictures — see OCCASION_EMOJI.
WORKOUT_DONE = "workout_done"
ACHIEVEMENT = "achievement"
GREETING = "greeting"
NUDGE = "nudge"
JAB = "jab"
WIN_BACK = "win_back"
PROGRESS = "progress"
# Deliberately absent from OCCASION_EMOJI below: the weekly sticker-only push
# is the one moment with nothing to say, so it draws from the whole pack.
RANDOM = "random"

# Emoji each occasion prefers, best match first. Compared against the emoji the
# pack's author attached to each sticker, with variation selectors stripped.
OCCASION_EMOJI: dict[str, tuple[str, ...]] = {
    WORKOUT_DONE: ("💪", "🔥", "👏", "🏋", "😎", "✅"),
    ACHIEVEMENT: ("🏆", "🥇", "🔥", "💪", "👏", "🎉"),
    GREETING: ("👋", "🤝", "😎", "💪"),
    NUDGE: ("😤", "👀", "💪", "🔥", "☝"),
    JAB: ("🤨", "😑", "😏", "😴", "👀", "😤"),
    WIN_BACK: ("🤝", "👋", "💪", "😌"),
    PROGRESS: ("📈", "📊", "💪", "🔥"),
}

# How long a fetched pack is trusted before being re-fetched. Packs change
# rarely; a day keeps the bot off getStickerSet for essentially every send.
_CACHE_TTL_SECONDS = 24 * 60 * 60
# A failed fetch is remembered too, briefly — a deleted pack shouldn't mean an
# extra failing API call on every single push for the next 24 hours.
_FAILURE_TTL_SECONDS = 10 * 60

# file_id lists per pack name, with the time they were fetched.
_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}

# The last sticker sent to each chat, so the same picture doesn't land twice in
# a row when an occasion has several equally good candidates. Bounded because
# it's keyed by chat and this process may outlive many of them.
_MAX_TRACKED_CHATS = 1024
_last_sent: OrderedDict[int, str] = OrderedDict()


def is_configured() -> bool:
    return bool(config.STICKERS_ENABLED and config.STICKER_PACK_NAMES)


def _normalize_emoji(emoji: str | None) -> str:
    """Drop variation selectors and skin-tone modifiers so 🏋️‍♂️ matches 🏋."""
    if not emoji:
        return ""
    stripped = "".join(
        ch
        for ch in emoji
        if not ("︀" <= ch <= "️" or "\U0001f3fb" <= ch <= "\U0001f3ff")
    )
    return stripped.split("‍")[0]  # 🏋️‍♂️ -> 🏋


async def _load_pack(bot: Bot, name: str) -> list[tuple[str, str]]:
    """(file_id, normalized emoji) for one pack, cached. Empty list on failure."""
    cached = _cache.get(name)
    if cached is not None:
        fetched_at, stickers = cached
        ttl = _CACHE_TTL_SECONDS if stickers else _FAILURE_TTL_SECONDS
        if time.monotonic() - fetched_at < ttl:
            return stickers
    try:
        pack = await bot.get_sticker_set(name)
        stickers = [(s.file_id, _normalize_emoji(s.emoji)) for s in pack.stickers]
    except TelegramAPIError as e:
        logger.warning("Sticker pack %r unavailable: %s", name, e)
        stickers = []
    except Exception:
        logger.exception("Failed to load sticker pack %r", name)
        stickers = []
    _cache[name] = (time.monotonic(), stickers)
    return stickers


async def _all_stickers(bot: Bot) -> list[tuple[str, str]]:
    stickers: list[tuple[str, str]] = []
    for name in config.STICKER_PACK_NAMES:
        stickers.extend(await _load_pack(bot, name))
    return stickers


def reset_cache() -> None:
    """Drop the cached packs and the per-chat history — for tests."""
    _cache.clear()
    _last_sent.clear()


def _choose(stickers: list[tuple[str, str]], occasion: str, chat_id: int | None) -> str | None:
    """Best-matching file_id for the occasion, or None if the pack is empty.

    Walks the occasion's emoji in order and stops at the first one the pack
    actually has, so a pack with 💪 stickers never falls through to a random
    picture — and a pack with none of them still answers with something.
    """
    if not stickers:
        return None
    previous = _last_sent.get(chat_id) if chat_id is not None else None
    for emoji in OCCASION_EMOJI.get(occasion, ()):
        matches = [file_id for file_id, sticker_emoji in stickers if sticker_emoji == emoji]
        if matches:
            return random.choice([m for m in matches if m != previous] or matches)
    pool = [file_id for file_id, _ in stickers]
    return random.choice([p for p in pool if p != previous] or pool)


def _remember(chat_id: int, file_id: str) -> None:
    _last_sent[chat_id] = file_id
    _last_sent.move_to_end(chat_id)
    while len(_last_sent) > _MAX_TRACKED_CHATS:
        _last_sent.popitem(last=False)


async def send(bot: Bot, chat_id: int, occasion: str, *, silent: bool = True) -> bool:
    """Send one sticker for this occasion. False (and no exception) if it can't.

    "Can't" covers every ordinary reason — stickers switched off globally, no
    pack configured, the pack gone, the user having blocked the bot — because
    none of them are worth failing a workout or a push over.
    """
    if not is_configured():
        return False
    try:
        file_id = _choose(await _all_stickers(bot), occasion, chat_id)
        if file_id is None:
            return False
        await bot.send_sticker(chat_id=chat_id, sticker=file_id, disable_notification=silent)
    except Exception:
        logger.exception("Failed to send %s sticker to chat %s", occasion, chat_id)
        return False
    _remember(chat_id, file_id)
    return True


async def send_to_user(bot: Bot, telegram_id: int, occasion: str, *, silent: bool = True) -> bool:
    """Like send(), but respects the user's own "Стикеры" setting first."""
    if not is_configured():
        return False
    try:
        user = await db.get_user(telegram_id)
    except Exception:
        logger.exception("Failed to read sticker setting for user %s", telegram_id)
        return False
    if user is None or not user["stickers_enabled"]:
        return False
    return await send(bot, telegram_id, occasion, silent=silent)
