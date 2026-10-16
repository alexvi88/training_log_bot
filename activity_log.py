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

Лента одна на оба клиента. Действия из iOS-приложения пишет сюда же
REST-слой `/v1` (`api_v1_activity.py`), помечая их `source="ios"`; всё, что
пришло из Telegram, помечено `source="tg"`. Двух лент нет нарочно: вопрос «как
пользуются» задаётся про человека, а не про клиент, и один и тот же атлет за
день успевает и записать подход в приложении, и нажать кнопку в боте.

Что именно пишется: текст сообщения (или подпись к фото), для нетекстовых —
пометка типа («🎤 голосовое»), для нажатия — надпись на кнопке, которую человек
видел, и её callback_data. Файлы и фото сами по себе не сохраняются — только то,
что человек ввёл, и след того, куда он нажал.

Голосовое — исключение из «только пометка типа»: «🎤 голосовое» в ленте не
отвечает на вопрос, что человек СКАЗАЛ, а это ровно то, ради чего лента открыта.
Как только бот (или `/v1`) расшифровал запись, расшифровка ложится в ту же
строку ленты: «🎤 голосовое: <текст>», а не распознал — «🎤 голосовое (не
распознал)» (см. record_voice_transcript). Сам звук не хранится по-прежнему.
В продуктовые события (product_metrics) расшифровка не попадает никогда — как и
любой набранный текст.

Тексты в ленте не режутся до «начала»: сообщение и расшифровка хранятся целиком
до MAX_CONTENT_LEN, ответ тренера — целиком. В stdout строка та же, полная, с
переводами строк, экранированными как «\\n» (log_text): одно действие — одна
строка журнала.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import logging

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

import db
import product_metrics

logger = logging.getLogger(__name__)

# Жёсткий потолок того, что человек ввёл (сообщение, подпись, расшифровка
# голосового): Telegram сам не пропускает сообщение длиннее 4096, так что
# обычный текст доезжает до ленты целиком, а простыня из буфера обмена (CSV
# импорта через /v1) не раздувает базу. Раньше было 1000 и «начала хватает» —
# не хватало: владелец читал обрывки вопросов и не понимал, на что ответил тренер.
# Ответ тренера этим потолком не режется — он наш и пишется целиком.
MAX_CONTENT_LEN = 4000

# Голосовое в ленте: пометка, расшифровка после двоеточия, или честное «не
# распознал». Префикс общий у всех трёх — по нему строку голосового и находят.
VOICE_MARK = "🎤 голосовое"
VOICE_UNRECOGNIZED = f"{VOICE_MARK} (не распознал)"
# Сколько после самого сообщения расшифровка ещё ищет свою строку, когда
# контекста входа нет (см. record_voice_transcript).
VOICE_MATCH_WINDOW = dt.timedelta(minutes=10)

# Откуда пришло действие. Лента одна на оба клиента (см. db.user_events.source и
# api_v1_activity.py): без пометки «начал тренировку» из приложения неотличимо
# от того же из бота, и владелец продукта, глядя на ленту, видел только половину
# происходящего — ту, что из Telegram.
SOURCE_TG = "tg"
SOURCE_IOS = "ios"


def process_log_line(
    source: str, telegram_id: int, action: str, detail: str, outcome: str
) -> str:
    """Одна строка про действие в обычный лог процесса (stdout сервера).

    Формат ОДИН на оба источника нарочно: владелец смотрит stdout, а не только
    /activity, и `grep "| записал подход |"` должен находить и телеграмные
    строки, и айосные. Разные формы записи у бота и у REST-слоя превратили бы
    такой греп в два разных грепа, а сравнение источников — в ручную работу.
    """
    return (
        f"[{source}] user={telegram_id} | {log_text(action)} | {log_text(detail)} | "
        f"{log_text(outcome)}"
    )


def log_text(text: str | None) -> str:
    """Текст целиком для строки журнала: до MAX_CONTENT_LEN, переводы строк —
    «\\n», чтобы одно действие оставалось одной строкой `fly logs`."""
    text = _cap(text or "")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


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
# Нажатие нижней (reply) клавиатуры. Telegram шлёт его обычным сообщением с
# текстом подписи, и в ленте оно было неотличимо от набранного руками слова:
# утренний разбор два дня подряд читал «Workout»/«Тренировка» как свободный
# текст и делал вывод, что бот его никуда не мапит, — хотя это кнопка, и
# handlers/persistent_menu.py ловит её на обоих языках.
KIND_REPLY_BUTTON = "reply_button"
# Тренер не ответил: таймаут хода, обрыв стрима, упавший провайдер. Человек в
# этот момент видит «не смог», а в логе оставался один вопрос без ответа —
# ровно то же, что и «ответил, а человек ушёл». Утренний разбор из такой тишины
# делал вывод про лишний круг вопросов в опроснике, хотя круга не было: сборка
# программы просто не доехала.
KIND_AI_FAILED = "ai_failed"
# Ответ тренера, под которым появилось превью программы с кнопкой «Добавить
# себе». Отдельно от KIND_AI_REPLY, потому что по ленте «предложил программу, а
# человек не нажал» и «наговорил план текстом, кнопки не было» выглядели
# одинаково — а это разные поломки: первая про кнопку, вторая про то, что
# модель не вызвала propose_program.
KIND_AI_PROGRAM_OFFERED = "ai_program_offered"

# Нетекстовые сообщения: сам файл не хранится, но факт «прислал голосовое» —
# ровно та часть картины, которой иначе не видно.
_MEDIA_MARKS = (
    ("voice", VOICE_MARK),
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


def _cap(text: str) -> str:
    """Жёсткий потолок ввода — не «показать начало», а защита базы от простыни."""
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
        parts.append(_cap(text))
    return " ".join(parts) if parts else "(сообщение без текста)"


def is_plain_text(content: str) -> bool:
    """Запись KIND_MESSAGE — набранный текст, а не пометка вложения
    (describe_message: «🎤 голосовое», «🖼 фото …», «(сообщение без текста)»)."""
    if not content or content == "(сообщение без текста)":
        return False
    return not content.startswith(tuple(mark for _, mark in _MEDIA_MARKS))


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


def _reply_button_label(message: Message) -> str | None:
    """Подпись нижней кнопки, если сообщение — это её нажатие.

    Сравнение идёт через keyboards.BTN_* , которые понимают обе локали сразу
    (см. _ReplyButtonMatch): у человека, только что переключившего язык, внизу
    ещё лежит клавиатура на прежнем.
    """
    text = (message.text or "").strip()
    if not text:
        return None
    import keyboards

    for button in (keyboards.BTN_WORKOUT, keyboards.BTN_MENU, keyboards.BTN_AI):
        if button == text:
            return text
    return None


def message_props(message: Message, reply_button: bool) -> dict:
    """Свойства события `tg_message` (product_metrics): вид сообщения и имя
    команды — без текста, который человек набрал."""
    if reply_button:
        return {"type": "reply_button"}
    text = (message.text or "").strip()
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0].split("@", 1)[0][1:].lower()
        return {"type": "command", "command": command[:32]}
    return {"type": str(message.content_type or "unknown")[:32]}


async def record_message(message: Message) -> int | None:
    """Входящее сообщение в ленту; id записанной строки (или None)."""
    if message.from_user is None:
        return None
    label = _reply_button_label(message)
    await product_metrics.track(
        message.from_user.id,
        "tg_message",
        message_props(message, label is not None),
        platform=product_metrics.PLATFORM_TG,
    )
    if label is not None:
        await db.log_user_event(message.from_user.id, KIND_REPLY_BUTTON, label)
        return None
    return await db.log_user_event(message.from_user.id, KIND_MESSAGE, describe_message(message))


async def record_callback(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        return
    data = callback.data or ""
    label = button_label(callback)
    await product_metrics.track(
        callback.from_user.id,
        "tg_callback",
        # Без чисел: «hist:12» и «hist:13» — один экран, а не два.
        {"screen": ":".join(p for p in callback_prefix(data).split(":") if not p.isdigit())},
        platform=product_metrics.PLATFORM_TG,
    )
    # Кнопка под пушем — это тап по пушу (push_open, platform tg): так в
    # сводке видно, что Telegram-пуши тоже нажимают, а не только читают.
    message = callback.message
    await product_metrics.record_tg_push_tap(
        callback.from_user.id, getattr(message, "message_id", None) if message else None
    )
    await db.log_user_event(
        callback.from_user.id, KIND_CALLBACK, _cap(label or data or "(кнопка)"), data or None
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
    text = text.strip()
    await db.log_user_event(user_id, KIND_AI_REPLY, text)
    logger.info("%s", process_log_line(SOURCE_TG, user_id, text, KIND_AI_REPLY, "ok"))


async def record_ai_failure(user_id: int, exc: BaseException) -> None:
    """Ход тренера, кончившийся ничем, — с причиной, а не просто «пусто».

    Причину складываем ЗДЕСЬ, а не на месте вызова: строка админская, и в
    handlers/ai_trainer (модуль объявлен переведённым) русский литерал ронял бы
    храповик i18n — по делу, потому что отличить там админскую строку от
    пользовательской нельзя.
    """
    reason = "таймаут" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
    await db.log_user_event(user_id, KIND_AI_FAILED, _cap(f"тренер не ответил: {reason}"))


async def record_ai_program_offered(user_id: int, title: str) -> None:
    """Под ответом встала программа — с её названием, чтобы в ленте было видно
    ЧТО предложили, а не только что предложили.

    Заглушка для безымянной лежит здесь, а не на месте вызова: строка
    админская, а handlers/ai_trainer объявлен переведённым — русский литерал
    там роняет храповик i18n, и по делу (см. record_ai_failure).
    """
    await db.log_user_event(
        user_id,
        KIND_AI_PROGRAM_OFFERED,
        _cap(f"предложил программу: {title.strip() or 'без названия'}"),
    )


async def record_recovered_callback(callback: CallbackQuery) -> None:
    """Кнопка трекера без состояния, но с живой тренировкой — экран вернули."""
    if callback.from_user is None:
        return
    data = callback.data or ""
    await db.log_user_event(
        callback.from_user.id, KIND_CALLBACK_RECOVERED, callback_prefix(data), data or None
    )


# Куда положить расшифровку голосового, которое сейчас обрабатывается. Ставит
# вход: middleware бота (LogIncomingMessages — строка ленты уже записана, её id
# известен) и middleware `/v1` (api_v1_activity.LogApiActions — строка
# пишется ПОСЛЕ обработчика, расшифровка кладётся в слот и приклеивается к ней).
# Изменяемый словарь, а не значение: Starlette зовёт обработчик в дочерней
# задаче, и новое значение ContextVar оттуда наверх не вернулось бы, а
# изменение общего словаря — возвращается.
_voice_slot: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "activity_voice_slot", default=None
)


def open_voice_slot(**fields) -> tuple[dict, contextvars.Token]:
    """Слот под расшифровку на время одного входа; закрывать — close_voice_slot."""
    slot = dict(fields)
    return slot, _voice_slot.set(slot)


def close_voice_slot(token: contextvars.Token) -> None:
    _voice_slot.reset(token)


def voice_content(transcript: str | None, original: str = VOICE_MARK) -> str:
    """Строка ленты для голосового: «🎤 голосовое: <текст>» или «(не распознал)».

    original — то, что записал вход до расшифровки (у голосового с подписью это
    «🎤 голосовое <подпись>»): пометку меняем, подпись оставляем.
    """
    text = (transcript or "").strip()
    heard = f"{VOICE_MARK}: {text}" if text else VOICE_UNRECOGNIZED
    if original.startswith(VOICE_MARK):
        heard = heard + original[len(VOICE_MARK):]
    return _cap(heard)


async def record_voice_transcript(user_id: int | None, transcript: str | None) -> None:
    """Расшифровка голосового — в ту же строку ленты, что и само голосовое.

    None или пустая строка — «не распознал» (провайдер упал или запись немая).
    Зовётся из ai_trainer.transcribe_voice — единственной точки расшифровки и у
    бота (вопрос тренеру, подход голосом), и у `/v1` (`/ai/voice`,
    `/workouts/{id}/sets/voice`), — так что новый путь голоса получает это сам.
    Только в user_events: в product_metrics текст человека не кладётся никогда.
    Никогда не бросает — лента не повод сорвать человеку подход.
    """
    if user_id is None:
        return
    try:
        slot = _voice_slot.get()
        source = SOURCE_TG
        if slot is not None and slot.get("source") == SOURCE_IOS:
            # /v1: строки ещё нет, её запишет LogApiActions после обработчика.
            slot["user_id"] = user_id
            slot["voice"] = voice_content(transcript)
            return
        if slot is not None and slot.get("user_id") == user_id and slot.get("event_id"):
            content = voice_content(transcript, slot.get("content") or VOICE_MARK)
            await db.update_user_event_content(slot["event_id"], content)
        else:
            # Контекста входа нет (вызов не из апдейта) — ищем свежую строку
            # «🎤 голосовое» этого человека; не нашли — не выдумываем новую.
            content = voice_content(transcript)
            if not await db.update_latest_voice_event(
                user_id, VOICE_MARK, content, VOICE_MATCH_WINDOW
            ):
                return
        logger.info("%s", process_log_line(source, user_id, content, "voice_transcript", "ok"))
    except Exception:
        logger.exception("Failed to log voice transcript")


class LogIncomingMessages(BaseMiddleware):
    """Каждое входящее сообщение — в user_events, ещё до фильтров и хендлеров."""

    async def __call__(self, handler, event, data):
        token = None
        if isinstance(event, Message):
            try:
                event_id = await record_message(event)
                if getattr(event, "voice", None) is not None and event_id and event.from_user is not None:
                    _, token = open_voice_slot(
                        source=SOURCE_TG,
                        user_id=event.from_user.id,
                        event_id=event_id,
                        content=describe_message(event),
                    )
                logger.info(
                    "%s",
                    process_log_line(
                        SOURCE_TG,
                        event.from_user.id if event.from_user else 0,
                        describe_message(event),
                        KIND_MESSAGE,
                        "ok",
                    ),
                )
            except Exception:
                logger.exception("Failed to log user message")
        try:
            return await handler(event, data)
        finally:
            if token is not None:
                close_voice_slot(token)


class LogCallbackQueries(BaseMiddleware):
    """То же для нажатий: что нажали и чем это нажатие было для бота."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            try:
                await record_callback(event)
                logger.info(
                    "%s",
                    process_log_line(
                        SOURCE_TG,
                        event.from_user.id if event.from_user else 0,
                        button_label(event) or event.data or "(кнопка)",
                        f"{KIND_CALLBACK} {event.data or ''}".strip(),
                        "ok",
                    ),
                )
            except Exception:
                logger.exception("Failed to log user callback")
        return await handler(event, data)
