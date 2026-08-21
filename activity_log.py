"""Пишет в базу всё, что человек делает в боте: сообщения и нажатия кнопок.

Зачем это отдельно от всего остального. Админ и раньше видел историю тренировок,
пуши и диалоги с AI-тренером — но всё это результаты: что записалось, что
отправилось. По ним не видно ни пути к результату, ни того, что до результата не
дошло: набранный и не понятый парсером подход, брошенный на полпути мастер,
десять тапов по одной кнопке подряд. А вопрос «как пользуются» — ровно про это.

Как это устроено. Два outer-middleware (регистрируются в main()): outer —
потому что писать надо и то, чего не поймал ни один хендлер (стикер в ответ на
экран логирования, текст в состоянии, где его никто не ждёт). Событие
записывается до вызова хендлера: упавший хендлер — это как раз то, что хочется
увидеть в логе, а не то, что должно из него исчезнуть. Ошибка самой записи
глотается — лог действий не тот повод, чтобы ронять человеку тренировку.

Что именно пишется: текст сообщения (или подпись к фото), для нетекстовых —
пометка типа («🎤 голосовое»), для нажатия — надпись на кнопке, которую человек
видел, и её callback_data. Файлы и фото сами по себе не сохраняются — только то,
что человек ввёл, и след того, куда он нажал.
"""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

import db

logger = logging.getLogger(__name__)

# Простыня из буфера обмена (импорт CSV, длинная простыня для AI-тренера) не
# должна раздувать базу — для «что человек ввёл» начала хватает с запасом.
MAX_CONTENT_LEN = 1000

KIND_MESSAGE = "message"
KIND_CALLBACK = "callback"
# Ответ AI-тренера. Лента задумана как «что человек делает», и своей стороны
# разговора в ней не было вовсе: видно вопрос и видно следующий тап, а что бот
# ответил между ними — только в /ai_dialogs, отдельным экраном и без окружающих
# действий. Понять по такой ленте, ПОЧЕМУ человек нажал то, что нажал, нельзя.
KIND_AI_REPLY = "ai_reply"
# Тап по кнопке трекера, у которой не осталось состояния, но тренировка жива:
# фолбэк вернул экран (см. handlers/fallback._recover_live_workout). Отдельный
# вид, а не KIND_CALLBACK_UNHANDLED: под тем же видом это навсегда выглядело бы
# регрессом роутинга — и в /activity, и в утреннем разборе, и в счётчике
# протухших префиксов, — хотя человеку вернули рабочий экран.
KIND_CALLBACK_RECOVERED = "callback_recovered"
# Отдельный вид — для нажатий, до которых не дотянулся ни один обработчик
# (handlers/fallback.py). Обычная запись KIND_CALLBACK пишется для любого
# нажатия одинаково, живого или протухшего, поэтому по ней не отличить
# редкую устаревшую кнопку от вспышки одного и того же префикса — то есть
# от регресса роутинга. См. record_unhandled_callback.
KIND_CALLBACK_UNHANDLED = "callback_unhandled"

# Нетекстовые сообщения: сам файл не хранится, но факт «прислал голосовое» —
# ровно та часть картины, которой иначе не видно.
_MEDIA_MARKS = (
    ("voice", "🎤 голосовое"),
    ("video_note", "🎥 кружок"),
    ("photo", "🖼 фото"),
    ("document", "📎 файл"),
    ("sticker", "🌀 стикер"),
    ("audio", "🎵 аудио"),
    ("video", "📹 видео"),
    ("animation", "🎞 гифка"),
    ("location", "📍 геопозиция"),
    ("contact", "👤 контакт"),
)


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_CONTENT_LEN:
        return text
    return text[: MAX_CONTENT_LEN - 1] + "…"


def describe_message(message: Message) -> str:
    """Одна строка про входящее сообщение — текст, подпись или пометка типа."""
    text = message.text or message.caption
    parts = []
    for attr, mark in _MEDIA_MARKS:
        if getattr(message, attr, None):
            parts.append(mark)
            break
    if text:
        parts.append(_truncate(text))
    return " ".join(parts) if parts else "(сообщение без текста)"


def button_label(callback: CallbackQuery) -> str | None:
    """Надпись нажатой кнопки — по callback_data ищем её в клавиатуре экрана.

    Именно надпись отвечает на вопрос «что человек нажал»: callback_data вроде
    `wo:set:12:3` говорит это только тому, кто помнит схему колбэков, а
    клавиатура у сообщения — прямо здесь, и брать её ниоткуда не надо.
    """
    markup = getattr(getattr(callback, "message", None), "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return None
    for row in rows:
        for button in row:
            if getattr(button, "callback_data", None) == callback.data:
                return button.text
    return None


async def record_message(message: Message) -> None:
    if message.from_user is None:
        return
    await db.log_user_event(message.from_user.id, KIND_MESSAGE, describe_message(message))


async def record_callback(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    data = callback.data or ""
    label = button_label(callback)
    await db.log_user_event(
        callback.from_user.id, KIND_CALLBACK, _truncate(label or data or "(кнопка)"), data or None
    )


def callback_prefix(data: str) -> str:
    """Первые два сегмента callback_data («hist:page», «wo:set») — достаточно
    грубо, чтобы сгруппировать нажатия по экрану, но не по конкретному id."""
    return ":".join(data.split(":")[:2]) if data else "(пусто)"


async def record_unhandled_callback(callback: CallbackQuery) -> None:
    """Нажатие, до которого не дотянулся ни один обработчик — отдельно от
    обычных KIND_CALLBACK, чтобы вспышка одного префикса (регресс роутинга)
    была видна отдельно от фонового шума легитимно устаревших экранов."""
    if callback.from_user is None:
        return
    data = callback.data or ""
    await db.log_user_event(
        callback.from_user.id, KIND_CALLBACK_UNHANDLED, callback_prefix(data), data or None
    )


async def record_ai_reply(user_id: int, text: str) -> None:
    """Ответ тренера — в ту же ленту, рядом с вопросом, на который он отвечает.

    Пишется там же, где ответ ложится в постоянный лог диалога
    (handlers/ai_trainer), а не из middleware: middleware видит входящие
    события, а это исходящее.
    """
    await db.log_user_event(user_id, KIND_AI_REPLY, _truncate(text))


async def record_recovered_callback(callback: CallbackQuery) -> None:
    """Кнопка трекера без состояния, но с живой тренировкой — экран вернули."""
    if callback.from_user is None:
        return
    data = callback.data or ""
    await db.log_user_event(
        callback.from_user.id, KIND_CALLBACK_RECOVERED, callback_prefix(data), data or None
    )


class LogIncomingMessages(BaseMiddleware):
    """Каждое входящее сообщение — в user_events, ещё до фильтров и хендлеров."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            try:
                await record_message(event)
            except Exception:
                logger.exception("Failed to log user message")
        return await handler(event, data)


class LogCallbackQueries(BaseMiddleware):
    """То же для нажатий: что нажали и чем это нажатие было для бота."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            try:
                await record_callback(event)
            except Exception:
                logger.exception("Failed to log user callback")
        return await handler(event, data)
