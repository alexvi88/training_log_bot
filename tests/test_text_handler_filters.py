"""No text-reading handler may be reachable by a message without text.

These handlers call message.text.strip() (or hand message.text to a parser)
straight away. Registered without an F.text filter they also matched stickers,
photos and voice messages, where message.text is None — so sending a sticker at,
say, the "введи дату" prompt raised AttributeError and the user got
"⚠️ Что-то пошло не так" instead of falling through to the normal fallback.

The check is on the *registered filters*, because that's what the crash depended
on: the unit tests around these handlers call them directly and would keep
passing either way.
"""

import inspect
import re
from types import SimpleNamespace

import pytest

from handlers import (
    backfill,
    bodyweight,
    csv_import,
    edit_workout,
    exercise_resolve,
    exercises,
    food_diary,
    history,
    routines,
    workout,
)

_MODULES = [
    backfill, bodyweight, csv_import, edit_workout, exercise_resolve,
    exercises, food_diary, history, routines, workout,
]

# Reading message.text, or handing it to something that will.
_READS_TEXT = re.compile(
    r"message\.text|parse_sets_line\(message|parse_single_token\(message"
    r"|parse_ru_date\(message|parse_bodyweight\(message"
)


def _text_reading_handlers():
    for module in _MODULES:
        for handler in module.router.message.handlers:
            try:
                source = inspect.getsource(handler.callback)
            except (OSError, TypeError):
                continue
            if _READS_TEXT.search(source):
                yield module.__name__, handler


def _rejects(handler, message) -> bool:
    """Whether any of the handler's own filters turns this message away.

    Only the magic filters (F.text and friends) are consulted — a StateFilter
    can't be evaluated without a real FSM context, and it isn't what guards
    against a text-less message anyway.
    """
    from aiogram.utils.magic_filter import MagicFilter

    for f in handler.filters or []:
        target = getattr(f.callback, "__self__", None)
        if isinstance(target, MagicFilter) and not f.callback(message):
            return True
    return False


@pytest.mark.parametrize(
    "module_name, handler",
    [(m, h) for m, h in _text_reading_handlers()],
    ids=lambda v: getattr(getattr(v, "callback", None), "__name__", str(v)),
)
def test_text_reading_handlers_reject_messages_without_text(module_name, handler):
    sticker = SimpleNamespace(text=None, caption=None, sticker=object(), photo=None, voice=None)
    assert _rejects(handler, sticker), (
        f"{module_name}.{handler.callback.__name__} reads message.text "
        f"but accepts a message that has none"
    )
