"""Shared helper for keeping bot screens at the bottom of the chat."""

import asyncio
import logging
import re
from contextlib import suppress

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InaccessibleMessage,
    InputMediaPhoto,
    Message,
)

import chat_bottom
import formatting

logger = logging.getLogger(__name__)

_NOT_MODIFIED = "message is not modified"

# Потолки Telegram: 4096 на текст сообщения и всего 1024 на подпись к фото. Берём
# те же константы, по которым сборщики экранов подгоняют содержимое
# (formatting.MESSAGE_LIMIT/CAPTION_LIMIT) — второй набор чисел про то же самое
# рано или поздно разъедется с первым.
TEXT_LIMIT = formatting.MESSAGE_LIMIT
CAPTION_LIMIT = formatting.CAPTION_LIMIT

# Пометка вместо молчаливой обрезки: экран, у которого просто нет конца, читается
# как потерянные данные, а не как «не поместилось».
_TRUNCATED_NOTE = "\n…дальше не влезло, обрезал"

# Последний рубеж, когда экран не удалось отправить ни в каком виде: сообщение
# уже удалено, поэтому без этого человек остаётся с пустым чатом без кнопок.
# Это чистый текст без своей клавиатуры — сюда попадаем, только когда и попытка
# с reply_markup уже провалилась, так что вместо новой инлайн-кнопки отправляем
# к уже нарисованной снизу постоянной кнопке «Меню» (keyboards.persistent_menu) —
# она никуда не девается, даже если этот текст не долетит с разметкой.
_RESCUE_TEXT = "⚠️ Экран не открылся. Жми «Меню» внизу под чатом."

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w-]*)[^>]*>")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+|#[xX][0-9a-fA-F]+);")


def _length(text: str, is_html: bool) -> int:
    """Длина в понимании Telegram, а не в символах Python.

    Считать по len() нельзя в обе стороны: лимит измеряется в кодовых единицах
    UTF-16 (эмодзи стоит две, и экран из эмодзи упирается в потолок вдвое
    раньше), а при parse_mode=HTML теги в лимит не входят вовсе — они уезжают в
    entities, и по len() мы бы резали экран, который ещё спокойно влезает.
    """
    if is_html:
        return formatting.telegram_length(text)
    return len(text.encode("utf-16-le")) // 2


def _unclosed_tags(html: str) -> list[str]:
    """Открытые и не закрытые в `html` теги — снизу вверх по вложенности.

    В HTML-разметке Telegram одиночных тегов нет вовсе (<br> и подобные не
    поддерживаются), так что любой открытый тег обязан быть закрыт — иначе
    Telegram отвечает «can't parse entities» и сообщение не уходит совсем.
    """
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        name = match.group(2).lower()
        if match.group(1):
            # Закрывающий тег: снимаем ближайший одноимённый открытый. Мусорный
            # </b> без пары просто игнорируем — не наше дело чинить разметку,
            # наше дело не сделать её хуже обрезкой.
            if name in stack:
                while stack.pop() != name:
                    pass
        else:
            stack.append(name)
    return stack


def _drop_dangling(head: str) -> str:
    """Убрать обрубок тега или HTML-сущности на конце — их Telegram не разберёт."""
    start = head.rfind("<")
    if start != -1 and ">" not in head[start:]:
        head = head[:start]
    start = head.rfind("&")
    if start != -1 and ";" not in head[start:]:
        head = head[:start]
    return head


def _hard_cut(text: str, budget: int, is_html: bool) -> str:
    """Обрезка внутри строки — на случай, когда одна строка сама длиннее лимита.

    Единственное место, где мы рвём строку посередине (простыня описания, ответ
    AI-тренера — там переводов строк может не быть вовсе). Идём по тексту
    кусками, а не по символам, чтобы точка обрыва никогда не попала внутрь тега
    или &-сущности: такой обрубок Telegram не разберёт и отвергнет сообщение
    целиком — то есть ровно то, от чего мы тут защищаемся.
    """
    used = 0
    index = 0
    while index < len(text):
        tag = _TAG_RE.match(text, index) if is_html else None
        if tag:
            index = tag.end()  # разметка в лимит не входит — берём тег целиком
            continue
        entity = _ENTITY_RE.match(text, index) if is_html else None
        chunk = entity.group(0) if entity else text[index]
        cost = 1 if entity else len(chunk.encode("utf-16-le")) // 2
        if used + cost > budget:
            break
        used += cost
        index += len(chunk)
    return text[:index]


def fit_to_limit(text: str, limit: int, parse_mode=None) -> str:
    """Текст, гарантированно влезающий в `limit`, с честной пометкой об обрезке.

    Почему это важно: превышение лимита — это TelegramBadRequest на отправке, а
    отправляем мы уже после удаления старого экрана, так что человек остаётся не
    с длинным экраном, а вообще без экрана. Для живого трекера тренировки это
    значит остаться без трекера до её конца. Обрезанный экран лучше пропавшего.

    Режем по границе строк: у нас все экраны построчные (таблицы подходов, списки
    упражнений), и строка, оборванная на середине, читается как баг. Незакрытые
    теги в остатке дописываем, а не дорезаем до них: <b>, открытый в первой
    строке, увёл бы обрезку в пустой текст. Закрывающие теги при этом бесплатны —
    разметка в лимит не считается.
    """
    is_html = isinstance(parse_mode, str) and parse_mode.lower() == "html"
    if text is None or _length(text, is_html) <= limit:
        return text
    budget = limit - _length(_TRUNCATED_NOTE, is_html)
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = _length(line, is_html) + (1 if kept else 0)  # +1 — сам перевод строки
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    head = "\n".join(kept) if kept else _hard_cut(text, budget, is_html)
    head = head.rstrip()
    if is_html:
        # Строку с "\n" внутри тега мы бы порвали по этому "\n" — подстраховка.
        head = _drop_dangling(head)
        head += "".join(f"</{tag}>" for tag in reversed(_unclosed_tags(head)))
    return head + _TRUNCATED_NOTE


async def _edit_text(message: Message, text: str, reply_markup, parse_mode) -> Message | None:
    """Edit in place; None means "couldn't — fall back to delete and resend"."""
    try:
        edited = await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        # Identical content: the screen already shows exactly what we wanted.
        return message if _NOT_MODIFIED in str(e).lower() else None
    return edited if isinstance(edited, Message) else message


async def _edit_photo(
    message: Message, photo: bytes, filename: str, caption: str, reply_markup, parse_mode
) -> Message | None:
    try:
        edited = await message.edit_media(
            InputMediaPhoto(
                media=BufferedInputFile(photo, filename=filename),
                caption=caption,
                parse_mode=parse_mode,
            ),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        return message if _NOT_MODIFIED in str(e).lower() else None
    return edited if isinstance(edited, Message) else message


async def _send_screen(callback: CallbackQuery, message: Message, text: str, reply_markup, parse_mode) -> Message:
    """Отправить новый экран так, чтобы чат не остался пустым.

    К этому моменту старое сообщение обычно уже удалено, поэтому любая ошибка
    здесь — это человек без экрана. Длину мы уже подрезали, но отказать Telegram
    может и по разметке, и по клавиатуре, так что откатываемся по шагам: тот же
    текст без parse_mode (сырые теги некрасивы, но экран на месте), а в самом
    конце — короткое сообщение с выходом в меню. Отправляем через bot по chat_id:
    у message.answer тот же адресат, но незачем зависеть от объекта сообщения,
    которого в чате уже нет.
    """
    try:
        return await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        logger.warning("Экран не отправился (%s) — спасаем чат", e.message)
    chat_id = message.chat.id
    with suppress(TelegramBadRequest):
        return await callback.bot.send_message(chat_id, text, reply_markup=reply_markup)
    return await callback.bot.send_message(chat_id, _RESCUE_TEXT)


async def safe_edit(
    callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None, delete: bool = True
) -> Message:
    """Show `text` as the bottom-most screen in the chat.

    Editing the callback's message in place is the cheap, flicker-free path, but
    Telegram can't move an edited message down past newer ones — so it's only
    safe while that message is still the last one in the chat (chat_bottom
    tracks that). Once anything has landed below it — a typed set, a record kept
    with its 🔥, a push — an in-place edit would leave a stale screen stranded
    above, so the message is deleted and a fresh one sent instead, putting the
    screen back under the user's thumb.

    delete=False keeps the callback's message intact — for screens like the
    AI-тренер chat, where that message is part of the user's conversation
    history, not a disposable menu screen.
    """
    message = callback.message
    # Подрезаем ДО того, как что-то удалим: раньше слишком длинный текст падал уже
    # на отправке нового экрана, то есть после delete() старого — и человек
    # оставался вообще без экрана (посреди тренировки — без трекера).
    text = fit_to_limit(text, TEXT_LIMIT, parse_mode)
    # InaccessibleMessage: так Telegram отдаёт сообщение, которое слишком старое
    # или удалено — а кнопки живут в истории чата вечно, так что это обычный
    # случай, а не край. Это не Message: у него нет ни `text`, ни `answer`, ни
    # `delete`, и обращение к ним даёт AttributeError вместо экрана. Править
    # нечего, поэтому просто отправляем новый экран в тот же чат.
    if isinstance(message, InaccessibleMessage):
        return await callback.bot.send_message(
            message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    if delete:
        # A photo message can't be edited into a text one, only replaced.
        if message.text is not None and chat_bottom.is_at_bottom(message.chat.id, message.message_id):
            edited = await _edit_text(message, text, reply_markup, parse_mode)
            if edited is not None:
                return edited
        with suppress(TelegramBadRequest):
            await message.delete()
    return await _send_screen(callback, message, text, reply_markup, parse_mode)


async def safe_edit_photo(
    callback: CallbackQuery,
    photo: bytes,
    filename: str,
    caption: str,
    reply_markup=None,
    parse_mode=None,
    delete: bool = True,
) -> Message:
    """Same idea as safe_edit, but for screens whose new content is a photo.

    Swapping the media of a photo message keeps chart navigation flicker-free
    while the screen is still at the bottom; otherwise (or when the current
    screen is text, which can't become a photo) the message is deleted and the
    chart sent as a fresh one, so repeated navigation doesn't leave a trail of
    stale photos behind. delete=False preserves the callback's message — see
    safe_edit.
    """
    message = callback.message
    # У подписи лимит вчетверо меньше, чем у текста, так что упереться в него
    # проще; всё остальное — как в safe_edit: режем до любых удалений.
    caption = fit_to_limit(caption, CAPTION_LIMIT, parse_mode)
    # То же, что в safe_edit: у InaccessibleMessage нет ни `photo`, ни методов.
    if isinstance(message, InaccessibleMessage):
        return await callback.bot.send_photo(
            message.chat.id,
            BufferedInputFile(photo, filename=filename),
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    if delete:
        if message.photo and chat_bottom.is_at_bottom(message.chat.id, message.message_id):
            edited = await _edit_photo(message, photo, filename, caption, reply_markup, parse_mode)
            if edited is not None:
                return edited
        with suppress(TelegramBadRequest):
            await message.delete()
    # Та же лестница откатов, что в _send_screen: подпись без разметки, а если и
    # фото не уходит — хотя бы текстовый экран с выходом в меню.
    try:
        return await message.answer_photo(
            BufferedInputFile(photo, filename=filename),
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as e:
        logger.warning("Фото-экран не отправился (%s) — спасаем чат", e.message)
    chat_id = message.chat.id
    with suppress(TelegramBadRequest):
        return await callback.bot.send_photo(
            chat_id,
            BufferedInputFile(photo, filename=filename),
            caption=caption,
            reply_markup=reply_markup,
        )
    return await callback.bot.send_message(chat_id, _RESCUE_TEXT)

# ---------- недолговечные ответы на неудачный ввод ----------

# Сколько живёт сообщение о непонятом вводе. Успешный ввод из чата удаляется
# сразу, а ошибка раньше оставалась навсегда — после трёх опечаток подряд экран
# с кнопками уезжал вверх за три простыни с примерами. Полминуты хватает
# прочитать и исправиться.
INPUT_ERROR_LIFETIME_SECONDS = 30

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Запустить в фоне, удерживая ссылку до завершения."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _delete_later(bot, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    with suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def reply_transient(message, text: str) -> None:
    """Ответить на неудачный ввод так, чтобы он не остался в чате навсегда.

    Убираем обе стороны разговора — и подсказку, и сам неудачный ввод: подсказка
    цитирует его, так что пока она висит, видно и что написали, и почему не
    вышло, а потом чат снова состоит из одних записей.

    Живёт здесь, а не в handlers/workout: тем же самым болели редактор прошлой
    тренировки, запись задним числом и дневник веса — там ошибки ввода копились
    в чате, потому что помощник лежал в чужом модуле.
    """
    reply = await message.reply(text)
    _spawn(_delete_later(message.bot, reply.chat.id, reply.message_id, INPUT_ERROR_LIFETIME_SECONDS))
    _spawn(
        _delete_later(message.bot, message.chat.id, message.message_id, INPUT_ERROR_LIFETIME_SECONDS)
    )


# ---------- «экран устарел»: карточка ссылается на удалённую запись ----------

# Карточки упражнения и тренировки открываются по id, зашитому в кнопку — если
# запись успели удалить или смержить где-то ещё, старая кнопка ведёт в никуда.
# Безличное «не найдено» не говорит, что делать дальше (TONE_OF_VOICE:
# «Ошибки: что случилось + как исправить»), поэтому оба текста называют экран,
# который надо открыть заново, вместо того чтобы просто констатировать факт.
EXERCISE_NOT_FOUND_TEXT = "Не нашёл это упражнение — экран устарел. Открой ⚙️ Упражнения заново"
WORKOUT_NOT_FOUND_TEXT = "Не нашёл эту тренировку — экран устарел. Открой 📚 Историю заново"


async def alert_exercise_not_found(callback) -> None:
    """Кнопка карточки упражнения ссылается на id, которого больше нет."""
    await callback.answer(EXERCISE_NOT_FOUND_TEXT, show_alert=True)


async def alert_workout_not_found(callback) -> None:
    """Кнопка карточки тренировки ссылается на id, которого больше нет."""
    await callback.answer(WORKOUT_NOT_FOUND_TEXT, show_alert=True)
