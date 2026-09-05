"""Catch-all for input that doesn't match any state-specific handler.

Registered last in main.py so every other router gets first refusal; only text
typed with no active flow (or in a state with no dedicated text handler, e.g.
main menu, group pickers) ends up here instead of being silently dropped.

Кнопки — та же история, и до этого модуля они не доходили вовсе. Больше сотни
обработчиков стоят под `StateFilter`, то есть после `state.clear()` (а он
случается на каждом `/start`, при завершении тренировки и при карантине
файлового хранилища) их callback не подхватывает никто. Telegram при этом ждёт
`answer()` — и без него человек десяток секунд смотрит на крутящуюся кнопку,
после чего не происходит ничего. В логах тоже пусто: необработанный callback не
идёт в `dp.errors`. Это и есть «бот залип».
"""

import logging
from contextlib import suppress
from types import SimpleNamespace
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import activity_log
import ai_trainer
import db
import i18n
from fsm import AITrainerFlow

# Функции, а не текстовые константы — см. комментарий над импортом
# ai_trainer_handlers в handlers/persistent_menu.py: константа, взятая по
# имени на верхнем уровне, замораживает язык на дефолте навсегда.
from handlers import ai_trainer as ai_trainer_handlers
from handlers import persistent_menu

router = Router(name="fallback")

logger = logging.getLogger(__name__)


class _CallbackAsMessage:
    """CallbackQuery → форма Message, которой ждут экраны workout.py.

    Обратная пара к `persistent_menu._MessageAsCallback`. Отправляем через
    `bot.send_message`, а не через `callback.message.answer`, нарочно: сообщение
    с кнопкой может приехать как `InaccessibleMessage` (Telegram отдаёт его для
    слишком старых или удалённых сообщений), а у него методов `answer` нет вовсе
    — только чат и id.
    """

    def __init__(self, callback: CallbackQuery):
        self._bot = callback.bot
        self._chat_id = callback.message.chat.id
        self.from_user = callback.from_user
        self.chat = callback.message.chat

    async def answer(self, text: str, **kwargs):
        return await self._bot.send_message(self._chat_id, text, **kwargs)

    async def answer_photo(self, photo, **kwargs):
        return await self._bot.send_photo(self._chat_id, photo, **kwargs)


# То, что человек послал сам и на что ответить «Не понял» уместно. Всё
# остальное — служебные апдейты чата: закрепил сообщение, сменил фото чата,
# включил таймер удаления. На них бот отвечал «Не понял 🤔», то есть огрызался
# на действие, которого никто не совершал (человек закрепил сообщение — и
# получил выговор). Перечисляем разрешённое, а не запрещённое: служебных типов
# у Telegram шесть десятков и они прибывают с каждой версией API.
_HUMAN_CONTENT = frozenset({
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.VIDEO_NOTE,
    ContentType.ANIMATION, ContentType.VOICE, ContentType.AUDIO, ContentType.DOCUMENT,
    ContentType.STICKER,
})


# Дешёвый (без единого платного вызова) детерминированный маршрут для текста
# из главного меню — до того, как всё уйдёт в общее «Не понял». Два случая,
# которые различимы без всякого AI:
#
#  а) текст похож на подход («жим 100 8», «100 8») — писать его сюда просто
#     ещё некуда, тренировка не начата;
#  б) текст — название (или обрывок названия) уже заведённого упражнения —
#     находим его тем же поиском, что и «⚙️ Упражнения», и даём кнопку сразу
#     на карточку, а не заставляем идти туда руками.
#
# Команды ("/xyz") через оба пути не пропускаются: тот же генеральный ответ,
# что и раньше — если команда не опознана нигде выше, разбираться в ней тут
# не наше дело.

# Порог, начиная с которого непонятый текст выглядит как вопрос тренеру, а не
# как случайная опечатка или обрывок слова — «жим», «100» короче него.
_ASK_TRAINER_MIN_LENGTH = 15

# Кнопка «🤖 Спросить тренера» под длинным непонятым текстом. Сам текст в
# callback_data не кладём (лимит Telegram — 64 байта, а вопрос может быть
# длинным) — он едет в FSM (`_ASK_PENDING_KEY`), кнопка только просит его
# оттуда забрать.
_ASK_CALLBACK = "fb:ask"
_ASK_PENDING_KEY = "fb_ask_pending"


async def _offer_ask_coach(message: Message, state: FSMContext) -> None:
    """Голосовое или фото без ожидающего состояния (голос иначе разбирается
    только в подходе и в чате с AI-тренером, см. handlers/ai_trainer.py) —
    вместо огульного «Не понял» предлагаем спросить тренера тем же разбором,
    что и в чате (ai_voice_question/ai_photo_question). Разбор не платный сам
    по себе (см. fb_ask_coach) — уходит только по тапу, а не на каждое
    случайно попавшее фото.
    """
    if message.content_type == ContentType.VOICE:
        kind, file_id = "voice", message.voice.file_id
        file_size, duration, caption = message.voice.file_size, message.voice.duration, ""
    else:
        kind = "photo"
        photo = message.photo[-1]
        file_id, file_size, duration = photo.file_id, photo.file_size, None
        caption = message.caption or ""
    await state.update_data(
        fb_media_kind=kind, fb_file_id=file_id, fb_file_size=file_size,
        fb_duration=duration, fb_caption=caption, fb_message_id=message.message_id,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=i18n.t("btn.ask_coach_about_this"), callback_data="fb:ask_coach")
    ]])
    await message.reply(
        i18n.t("fallback.media_unclear", btn=i18n.t("btn.ask_coach_about_this")),
        reply_markup=kb,
    )


class _ReplayedMedia:
    """Голосовое/фото, вызванное по кнопке «🤖 Спросить тренера про это», в
    форме, которой ждут ai_voice_question/ai_photo_question (handlers/ai_trainer.py)
    — тех же атрибутов, что у исходного Message, только собранных заново из
    FSM: у Bot API нет способа получить старое сообщение по id, а гонять сам
    объект Message через FSM (JSON) нельзя.
    """

    def __init__(
        self, callback: CallbackQuery, *, kind: str, file_id: str,
        file_size: Optional[int], duration: Optional[int], caption: str,
    ):
        self._bot = callback.bot
        self._chat_id = callback.message.chat.id
        self.bot = callback.bot
        self.from_user = callback.from_user
        self.chat = callback.message.chat
        self.caption = caption or None
        if kind == "voice":
            self.voice = SimpleNamespace(file_id=file_id, file_size=file_size, duration=duration)
            self.photo = None
        else:
            self.photo = [SimpleNamespace(file_id=file_id, file_size=file_size)]
            self.voice = None

    async def reply(self, text: str, **kwargs):
        return await self._bot.send_message(self._chat_id, text, **kwargs)

    async def answer(self, text: str, **kwargs):
        return await self._bot.send_message(self._chat_id, text, **kwargs)


@router.callback_query(F.data == "fb:ask_coach")
async def fb_ask_coach(callback: CallbackQuery, state: FSMContext) -> None:
    from fsm import AITrainerFlow
    from handlers.ai_trainer import ai_photo_question, ai_voice_question

    data = await state.get_data()
    kind, file_id = data.get("fb_media_kind"), data.get("fb_file_id")
    if not kind or not file_id:
        await callback.answer(i18n.t("fallback.ask_coach_expired"), show_alert=True)
        return
    wrapper = _ReplayedMedia(
        callback, kind=kind, file_id=file_id,
        file_size=data.get("fb_file_size"), duration=data.get("fb_duration"),
        caption=data.get("fb_caption") or "",
    )
    with suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await state.set_state(AITrainerFlow.chatting)
    await state.update_data(
        fb_media_kind=None, fb_file_id=None, fb_file_size=None,
        fb_duration=None, fb_caption=None, fb_message_id=None,
    )
    if kind == "voice":
        await ai_voice_question(wrapper, state)
    else:
        await ai_photo_question(wrapper, state)


@router.message()
async def unhandled_text(message: Message, state: FSMContext) -> None:
    if message.content_type not in _HUMAN_CONTENT:
        return
    if message.content_type in (ContentType.VOICE, ContentType.PHOTO):
        await _offer_ask_coach(message, state)
        return
    text = (message.text or "").strip() if message.content_type == ContentType.TEXT else ""
    if text and not text.startswith("/"):
        from handlers.workout import _looks_like_a_set

        if _looks_like_a_set(text):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=i18n.t("btn.start_workout_caps"), callback_data="menu:start_workout"
                )
            ]])
            await message.reply(
                i18n.t("fallback.looks_like_set", btn=i18n.t("btn.start_workout_caps")),
                reply_markup=kb,
            )
            return
        found = await db.search_exercises(message.from_user.id, text, limit=1)
        if found:
            ex = found[0]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"📋 {ex['display_name']}", callback_data=f"prog:card:{ex['id']}")
            ]])
            await message.reply(
                i18n.t("fallback.found_exercise", name=ex["display_name"]),
                reply_markup=kb,
            )
            return
        # Длинный непонятый текст, не подход и не название упражнения — почти
        # наверняка вопрос тренеру, напечатанный из главного меню («составь
        # мне программу»). Без единого платного вызова: квота и вся логика
        # AI-тренера тратятся только по тапу на кнопку, см. ask_trainer ниже.
        if len(text) > _ASK_TRAINER_MIN_LENGTH and ai_trainer.is_configured():
            await state.update_data(**{_ASK_PENDING_KEY: text})
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=i18n.t("btn.ask_trainer"), callback_data=_ASK_CALLBACK)
            ]])
            await message.reply(i18n.t("fallback.ask_prompt"), reply_markup=kb)
            return
    # Короткий непонятый текст (или AI-тренер не настроен) — прежний общий
    # ответ, подсказывающий дорогу к AI-тренеру через нижнюю клавиатуру.
    await message.reply(i18n.t("fallback.generic", ai_btn=i18n.t("btn.persistent.ai")))


@router.callback_query(F.data == _ASK_CALLBACK)
async def ask_trainer(callback: CallbackQuery, state: FSMContext) -> None:
    """Тап по «🤖 Спросить тренера» под непонятым текстом из главного меню.

    Отправляет ровно тот текст, что человек уже напечатал, тем же ходом, что
    и обычный вопрос из чата тренера (`ai_trainer.ai_question` →
    `_handle_question`) — лимиты и квоты проверяются там же, ни разу не своей
    проверкой на месте.
    """
    data = await state.get_data()
    question = (data.get(_ASK_PENDING_KEY) or "").strip()
    message = callback.message

    if callback.from_user.id in ai_trainer_handlers._busy:
        # Второй тап по той же кнопке, пока первый ещё думает: текст из FSM уже
        # забран первым тапом, и без этой ветки человек получал бы тост «вопрос
        # потерян», хотя ничего не потерялось — тренер отвечает.
        await callback.answer(i18n.t("ai.screen.busy"))
        return

    if not question:
        # Пока думал — переписался (перезапуск, другое сообщение): вопрос
        # потерян, честно говорим об этом тостом и открываем тренера как
        # обычно, а не молчим и не гадаем, что имелось в виду.
        await callback.answer(i18n.t("fallback.ask_stale"))
        if message is not None and hasattr(message, "answer"):
            await persistent_menu._open_ai_trainer(message, state)
        return

    await state.update_data(**{_ASK_PENDING_KEY: None})

    if not ai_trainer.is_configured() or message is None or not hasattr(message, "answer"):
        # Конфигурация выключилась между показом кнопки и тапом — тот же
        # генеральный ответ, что видел бы обычный атлет с самого начала.
        await callback.answer()
        if message is not None and hasattr(message, "answer"):
            await message.answer(i18n.t("persistent_menu.ai_not_configured"))
        return

    await callback.answer()
    await db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.language_code
    )
    from handlers.workout import _clear_state_keep_workout

    await _clear_state_keep_workout(state)
    await state.set_state(AITrainerFlow.chatting)

    user_id = callback.from_user.id
    if not ai_trainer_handlers._try_claim_busy(user_id):
        await message.answer(i18n.t("ai.screen.busy"))
        return
    try:
        await ai_trainer_handlers._handle_question(
            message, state, question, history_question=question, user_id=user_id
        )
    finally:
        ai_trainer_handlers._busy.discard(user_id)


# Кнопки живого трекера. Все они стоят под StateFilter (logging_set/idle), а
# состояние теряется от любого потока со своим: импорт CSV, мини-игра, опросник
# тренера. Тренировка при этом жива в базе — и человек, вернувшийся к своему же
# экрану и нажавший «✅ Закончить упражнение», попадал сюда: тост «кнопка
# устарела» и главное меню, то есть ровно то, что читается как «тренировку
# потеряли». Так это и выглядело в логе 19.08 (💀 live:finish_exercise).
_LIVE_PREFIX = "live:"


async def _recover_live_workout(callback: CallbackQuery, state: FSMContext) -> bool:
    """Кнопка трекера без состояния, но с живой тренировкой — вернуть трекер.

    True — разобрались здесь, общий ответ про устаревшую кнопку не нужен. Экран
    пересобирается тем же `_enter_live`, что и «Продолжить тренировку»: он
    поднимает открытые упражнения и остаток плана из базы, когда в FSM их уже
    нет. Само действие не выполняем — какое упражнение было активным, после
    потери состояния известно лишь по догадке из базы; человеку возвращается
    рабочий экран, и второй тап делает то, что он хотел.
    """
    if not (callback.data or "").startswith(_LIVE_PREFIX):
        return False
    message = getattr(callback, "message", None)
    # InaccessibleMessage (слишком старое или удалённое сообщение) не умеет
    # `answer`, а `_enter_live` отправляет через него новый экран.
    if message is None or not hasattr(message, "answer"):
        return False
    workout = await db.get_active_workout(callback.from_user.id)
    if workout is None:
        return False
    from handlers.workout import _enter_live

    await callback.answer(i18n.t("fallback.workout_recovered"))
    await _enter_live(callback, state, workout["id"], delete_message=False)
    return True


@router.callback_query()
async def unhandled_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка с экрана, чей поток уже закончился.

    Отвечаем не отпиской, а рабочим меню: экран в истории чата живёт вечно, и
    человек, ткнувший вчерашнюю кнопку, хочет продолжить, а не читать про
    «устаревшее состояние». Меню собирается тем же `cmd_start`, что и по команде,
    так что разъехаться им негде.
    """
    from handlers.workout import cmd_start

    # Данные кнопки в лог и в activity_log отдельным видом события: это
    # единственный след, по которому потом видно, какой экран остался без
    # обработчика (сам callback в dp.errors не попадает), и по которому можно
    # отличить редкую протухшую кнопку от вспышки одного префикса — регресса
    # роутинга (см. db.count_unhandled_callbacks_by_prefix).
    # Сначала выясняем, вернём ли мы тренировку: от этого зависит и вид события
    # в логе. Под общим «протухшая кнопка» восстановленный тап навсегда выглядел
    # бы регрессом роутинга — в /activity, в утреннем разборе и в счётчике
    # префиксов (db.count_unhandled_callbacks_by_prefix), — хотя человек получил
    # рабочий экран.
    recovered = await _recover_live_workout(callback, state)
    logger.info(
        "%s callback %s from user %s",
        "Recovered" if recovered else "Unhandled",
        callback.data,
        callback.from_user.id,
    )
    try:
        if recovered:
            await activity_log.record_recovered_callback(callback)
        else:
            await activity_log.record_unhandled_callback(callback)
    except Exception:
        logger.exception("Failed to log callback")
    if recovered:
        return
    await callback.answer(i18n.t("fallback.stale_button"))
    if callback.message is None:  # pragma: no cover — Telegram всегда даёт message
        return
    await cmd_start(_CallbackAsMessage(callback), state)
