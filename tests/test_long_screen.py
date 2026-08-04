"""Экран длиннее лимита Telegram обрезается, а не исчезает.

Раньше safe_edit сначала удалял старое сообщение и только потом отправлял новое:
если текст перевалил 4096 символов (или подпись — 1024), отправка падала уже
после удаления, и человек оставался вообще без экрана — посреди тренировки без
трекера до её конца. Здесь моки ведут себя как Telegram: слишком длинный текст
отвергают.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

import chat_bottom
import formatting
import ui

CHAT_ID = 555
SCREEN_ID = 20

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w-]*)[^>]*>")


@pytest.fixture(autouse=True)
def clean_tracker():
    chat_bottom.reset()
    yield
    chat_bottom.reset()


def _tags_balanced(html: str) -> bool:
    """Разметка, которую Telegram примет: каждый открытый тег закрыт, и наоборот."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        name = match.group(2).lower()
        if match.group(1):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _tg_length(text: str, parse_mode) -> int:
    """Длина так, как её считает Telegram: UTF-16, и теги при HTML не в счёт."""
    if isinstance(parse_mode, str) and parse_mode.lower() == "html":
        return formatting.telegram_length(text)
    return len(text.encode("utf-16-le")) // 2


def _like_telegram(limit: int, key: str = "text"):
    """Мок отправки, отвергающий слишком длинный текст, — как настоящий Telegram."""

    async def send(*args, **kwargs):
        # Текст приходит то позиционно (message.answer), то в kwargs (caption),
        # то вторым аргументом после chat_id (bot.send_message).
        value = kwargs.get(key)
        if value is None:
            positional = [arg for arg in args if isinstance(arg, str)]
            value = positional[-1] if positional else None
        if value is not None and _tg_length(value, kwargs.get("parse_mode")) > limit:
            raise TelegramBadRequest(method=MagicMock(), message=f"Bad Request: {key} is too long")
        return SimpleNamespace(message_id=SCREEN_ID + 1)

    return AsyncMock(side_effect=send)


def _make_callback(text: str | None = "экран", photo=None):
    message = MagicMock()
    message.chat = SimpleNamespace(id=CHAT_ID)
    message.message_id = SCREEN_ID
    message.text = text
    message.photo = photo
    message.edit_text = _like_telegram(ui.TEXT_LIMIT)
    message.edit_media = AsyncMock(return_value=True)
    message.delete = AsyncMock()
    message.answer = _like_telegram(ui.TEXT_LIMIT)
    message.answer_photo = _like_telegram(ui.CAPTION_LIMIT, key="caption")
    callback = MagicMock()
    callback.message = message
    callback.bot.send_message = _like_telegram(ui.TEXT_LIMIT)
    callback.bot.send_photo = _like_telegram(ui.CAPTION_LIMIT, key="caption")
    return callback


def _long_plain(lines: int = 400) -> str:
    return "\n".join(f"Приседания со штангой {i}: 100 кг × 5" for i in range(lines))


def _long_html(lines: int = 400) -> str:
    return "\n".join(f"<b>Приседания {i}</b>: <i>100 кг × 5</i>" for i in range(lines))


# ---------- сама обрезка ----------


def test_fit_to_limit_leaves_short_text_alone():
    assert ui.fit_to_limit("короткий экран", ui.TEXT_LIMIT) == "короткий экран"


def test_fit_to_limit_marks_the_cut():
    fitted = ui.fit_to_limit(_long_plain(), ui.TEXT_LIMIT)
    assert _tg_length(fitted, None) <= ui.TEXT_LIMIT
    assert fitted.endswith("обрезано")


def test_fit_to_limit_cuts_on_a_line_boundary():
    """Строка, оборванная на середине, читается как потерянные данные."""
    fitted = ui.fit_to_limit(_long_plain(), ui.TEXT_LIMIT)
    body = fitted[: fitted.rindex("\n")]
    assert all(line.endswith("100 кг × 5") for line in body.split("\n"))


def test_fit_to_limit_counts_emoji_as_telegram_does():
    """Лимит — в единицах UTF-16: экран из эмодзи упирается в него вдвое раньше,
    чем показывает len(), и раньше такой экран отвергался уже на отправке."""
    fitted = ui.fit_to_limit("🏋️" * 3000, ui.TEXT_LIMIT)
    assert _tg_length(fitted, None) <= ui.TEXT_LIMIT


def test_fit_to_limit_does_not_count_markup_toward_the_limit():
    """Экраны, которые сборщики уже подогнали под лимит по видимой длине, обрезать
    нечего: разметка уезжает в entities, и по len() мы съедали бы живой текст."""
    text = "\n".join("<b>Жим</b>: <i>100 кг</i>" for _ in range(80))
    assert len(text) > ui.CAPTION_LIMIT
    assert formatting.telegram_length(text) <= ui.CAPTION_LIMIT

    assert ui.fit_to_limit(text, ui.CAPTION_LIMIT, parse_mode="HTML") == text


def test_fit_to_limit_keeps_html_valid():
    fitted = ui.fit_to_limit(_long_html(), ui.TEXT_LIMIT, parse_mode="HTML")
    assert _tg_length(fitted, "HTML") <= ui.TEXT_LIMIT
    assert _tags_balanced(fitted)


def test_fit_to_limit_closes_a_tag_opened_before_the_cut():
    """Тег, открытый в первой строке, нельзя «дорезать» — только закрыть."""
    text = "<b>заголовок\n" + _long_plain() + "</b>"
    fitted = ui.fit_to_limit(text, ui.TEXT_LIMIT, parse_mode="HTML")
    assert fitted.startswith("<b>заголовок")
    assert _tg_length(fitted, "HTML") <= ui.TEXT_LIMIT
    assert _tags_balanced(fitted)


def test_fit_to_limit_never_leaves_half_a_tag():
    """Обрезка внутри «<b» или «&amp» — это «can't parse entities» на отправке."""
    one_line = "<b>" + "и" * 8000 + "</b>"
    fitted = ui.fit_to_limit(one_line, ui.TEXT_LIMIT, parse_mode="HTML")
    assert _tg_length(fitted, "HTML") <= ui.TEXT_LIMIT
    assert _tags_balanced(fitted)
    for chunk in ("&" + "и" * 8000, "и" * 5000 + "&amp"):
        fitted = ui.fit_to_limit(chunk, ui.TEXT_LIMIT, parse_mode="HTML")
        assert _tg_length(fitted, "HTML") <= ui.TEXT_LIMIT
        assert "&" not in fitted


def test_fit_to_limit_keeps_escaped_ampersands():
    """&amp; — это один видимый символ, а не повод дорезать текст до него."""
    fitted = ui.fit_to_limit("\n".join(["Жим &amp; тяга"] * 900), ui.TEXT_LIMIT, parse_mode="HTML")
    assert "&amp;" in fitted
    assert _tg_length(fitted, "HTML") <= ui.TEXT_LIMIT


def test_fit_to_limit_leaves_plain_text_tags_alone():
    """Без parse_mode «<b>» — это просто символы, дописывать к ним нечего."""
    fitted = ui.fit_to_limit("<b>" + "я" * 8000, ui.TEXT_LIMIT)
    assert "</b>" not in fitted
    assert _tg_length(fitted, None) <= ui.TEXT_LIMIT


# ---------- safe_edit ----------


async def test_safe_edit_survives_a_screen_longer_than_the_limit():
    """Главный баг: длинный экран не должен стоить человеку экрана вообще."""
    callback = _make_callback()

    sent = await ui.safe_edit(callback, _long_plain())

    assert sent is not None
    callback.message.answer.assert_awaited_once()
    text = callback.message.answer.await_args.args[0]
    assert _tg_length(text, None) <= ui.TEXT_LIMIT
    assert text.endswith("обрезано")


async def test_safe_edit_truncates_before_touching_the_old_screen():
    """Обрезка обязана случиться до delete(): иначе падение оставляет пустой чат."""
    callback = _make_callback()
    order: list[str] = []
    callback.message.delete = AsyncMock(side_effect=lambda: order.append("delete"))
    original_answer = callback.message.answer

    async def answer(*args, **kwargs):
        order.append("answer")
        return await original_answer(*args, **kwargs)

    callback.message.answer = AsyncMock(side_effect=answer)

    await ui.safe_edit(callback, _long_html(), parse_mode="HTML")

    assert order == ["delete", "answer"]
    assert _tg_length(callback.message.answer.await_args.args[0], "HTML") <= ui.TEXT_LIMIT


async def test_safe_edit_in_place_gets_the_truncated_text():
    """Путь редактирования тоже упирается в лимит — и тоже возвращал None."""
    chat_bottom.note_message(CHAT_ID, SCREEN_ID)
    callback = _make_callback()

    await ui.safe_edit(callback, _long_html(), parse_mode="HTML")

    callback.message.edit_text.assert_awaited_once()
    edited = callback.message.edit_text.await_args.args[0]
    assert _tg_length(edited, "HTML") <= ui.TEXT_LIMIT
    callback.message.delete.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


async def test_safe_edit_falls_back_to_bot_send_message():
    """Отказ по разметке уже после delete() — экран всё равно должен появиться."""
    callback = _make_callback()
    callback.message.answer = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: can't parse entities")
    )

    await ui.safe_edit(callback, "экран", parse_mode="HTML")

    callback.bot.send_message.assert_awaited_once()
    assert callback.bot.send_message.await_args.args[1] == "экран"


async def test_safe_edit_leaves_a_way_back_when_nothing_sends():
    callback = _make_callback()
    callback.message.answer = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: nope")
    )
    callback.bot.send_message = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=MagicMock(), message="Bad Request: nope"),
            SimpleNamespace(message_id=SCREEN_ID + 2),
        ]
    )

    await ui.safe_edit(callback, "экран", parse_mode="HTML")

    assert callback.bot.send_message.await_count == 2
    assert "/start" in callback.bot.send_message.await_args.args[1]


# ---------- safe_edit_photo ----------


async def test_safe_edit_photo_survives_a_caption_longer_than_the_limit():
    """У подписи лимит 1024, а не 4096 — упереться в него куда проще."""
    callback = _make_callback()

    sent = await ui.safe_edit_photo(callback, b"png", "chart.png", _long_html(), parse_mode="HTML")

    assert sent is not None
    caption = callback.message.answer_photo.await_args.kwargs["caption"]
    assert _tg_length(caption, "HTML") <= ui.CAPTION_LIMIT
    assert caption.endswith("обрезано")
    assert _tags_balanced(caption)


async def test_safe_edit_photo_leaves_a_way_back_when_nothing_sends():
    callback = _make_callback()
    callback.message.answer_photo = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: nope")
    )
    callback.bot.send_photo = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: nope")
    )

    await ui.safe_edit_photo(callback, b"png", "chart.png", "подпись")

    callback.bot.send_message.assert_awaited_once()
    assert "/start" in callback.bot.send_message.await_args.args[1]
