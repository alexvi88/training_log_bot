"""Безличное «Упражнение не найдено» / «Тренировка не найдена» не говорило,
что делать дальше — TONE_OF_VOICE требует называть и причину, и следующий шаг.
Оба алерта теперь идут через общий helper в ui.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import ui

pytestmark = pytest.mark.asyncio


def _make_callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    return callback


async def test_alert_exercise_not_found_names_the_screen_to_reopen():
    callback = _make_callback()

    await ui.alert_exercise_not_found(callback)

    callback.answer.assert_awaited_once_with(
        "Не нашёл это упражнение — экран устарел. Открой ⚙️ Упражнения заново",
        show_alert=True,
    )


async def test_alert_workout_not_found_names_the_screen_to_reopen():
    callback = _make_callback()

    await ui.alert_workout_not_found(callback)

    callback.answer.assert_awaited_once_with(
        "Не нашёл эту тренировку — экран устарел. Открой 📚 Историю заново",
        show_alert=True,
    )
