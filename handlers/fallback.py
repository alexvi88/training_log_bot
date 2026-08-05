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
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

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


@router.message()
async def unhandled_text(message: Message) -> None:
    # Сюда чаще всего прилетает вопрос тренеру, напечатанный из главного меню
    # («составь мне программу»), — подсказываем дорогу к AI-тренеру, а не только
    # /start. Без детекции по словам: любой непонятый текст получает один ответ.
    await message.reply(
        "Не понял 🤔 Вопрос тренеру — жми «AI-тренер» на клавиатуре снизу. Меню — /start"
    )


@router.callback_query()
async def unhandled_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка с экрана, чей поток уже закончился.

    Отвечаем не отпиской, а рабочим меню: экран в истории чата живёт вечно, и
    человек, ткнувший вчерашнюю кнопку, хочет продолжить, а не читать про
    «устаревшее состояние». Меню собирается тем же `cmd_start`, что и по команде,
    так что разъехаться им негде.
    """
    from handlers.workout import cmd_start

    # Данные кнопки в лог: это единственный след, по которому потом видно, какой
    # экран остался без обработчика (сам callback в dp.errors не попадает).
    logger.info("Unhandled callback %s from user %s", callback.data, callback.from_user.id)
    await callback.answer("Этот экран уже неактуален — открыл меню.")
    if callback.message is None:  # pragma: no cover — Telegram всегда даёт message
        return
    await cmd_start(_CallbackAsMessage(callback), state)
