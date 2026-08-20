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

from aiogram import Router
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import activity_log
import db
import i18n

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


@router.message()
async def unhandled_text(message: Message) -> None:
    if message.content_type not in _HUMAN_CONTENT:
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
    # Сюда чаще всего прилетает вопрос тренеру, напечатанный из главного меню
    # («составь мне программу»), — подсказываем дорогу к AI-тренеру, а не только
    # /start. Без детекции по словам: любой непонятый текст получает один ответ.
    await message.reply(i18n.t("fallback.generic", ai_btn=i18n.t("btn.persistent.ai")))


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
    logger.info("Unhandled callback %s from user %s", callback.data, callback.from_user.id)
    try:
        await activity_log.record_unhandled_callback(callback)
    except Exception:
        logger.exception("Failed to log unhandled callback")
    if await _recover_live_workout(callback, state):
        return
    await callback.answer(i18n.t("fallback.stale_button"))
    if callback.message is None:  # pragma: no cover — Telegram всегда даёт message
        return
    await cmd_start(_CallbackAsMessage(callback), state)
