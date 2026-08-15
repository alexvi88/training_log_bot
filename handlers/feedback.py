"""/feedback — free-form feedback (text, photos, whatever) relayed straight to the admin."""

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards
import state_scaffold
from fsm import FeedbackFlow

router = Router(name="feedback")

_PROMPT = (
    "Напиши что угодно — отзыв, баг, идею. Можно с фото или скриншотом, можно "
    "несколькими сообщениями подряд. Передам всё админу как есть.\n\n"
    "Передумал — «❌ Отмена» или любая команда, хоть /start."
)

# Роутер фидбека подключён вторым (см. main.setup_routers), чтобы /feedback
# долетал из любого состояния. Обратная сторона: пока человек ждёт-набирает
# отзыв, его «ловлю всё» стоит впереди команд всех остальных роутеров — и /start
# передумавшего уходил админу как текст отзыва. Поэтому команды отсекаем
# фильтром: не совпавший фильтр отдаёт апдейт следующему роутеру, в отличие от
# раннего return внутри хендлера. Тот же приём, что в food_diary._NOT_A_COMMAND.
_NOT_A_COMMAND = ~F.text.startswith("/")


class DropFeedbackStateOnCommand(BaseMiddleware):
    """Команда посреди отзыва — это выход из отзыва, одно место на все команды.

    Фильтра выше мало: он лишь отдаёт команду её собственному хендлеру, а тот
    снимает состояние, только если у него это заведено (/start, /mcp,
    /food_diary — да). Незнакомая боту команда — /cancel, /menu — доходит до
    fallback, и человек оставался в ожидании отзыва: следующая его реплика
    («а сколько мне есть белка?») снова молча уезжала админу. Хранилище файловое
    (fsm_storage.py), так что это переживало и перезапуск.

    Снимается только состояние отзыва и только аккуратно: отзыв пишут и посреди
    незакрытой тренировки, а `state.clear()` снёс бы её каркас (см.
    state_scaffold). Апдейт не задерживаем — дальше его забирает настоящий
    хендлер команды.

    Это не дубль routines.DropInputStateOnExit: тот снимает состояния ввода
    программ на выходящих кнопках своего роутера и до сообщений не касается.
    """

    async def __call__(self, handler, event, data):
        state: FSMContext | None = data.get("state")
        # Порядок проверок — от дешёвой к дорогой: через этот middleware идёт
        # каждое сообщение бота, а обращение к состоянию (и тем более его снятие,
        # это запись в файл) стоит дороже, чем взгляд на первый символ текста.
        if (
            state is not None
            and isinstance(event, Message)
            and (event.text or "").startswith("/")
            and await state.get_state() == FeedbackFlow.awaiting_message.state
        ):
            await state_scaffold.clear_state_keep_workout(state)
        return await handler(event, data)


# Через outer_middleware, а не middleware: снимать состояние надо и тогда, когда
# ни один хендлер этого роутера команду не забрал, — а именно этот случай и
# лечим.
router.message.outer_middleware(DropFeedbackStateOnCommand())


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    # Отзыв оставляют и в перерыве между подходами, так что каркас незакрытой
    # тренировки должен пережить вход сюда.
    await state_scaffold.clear_state_keep_workout(state)
    await state.set_state(FeedbackFlow.awaiting_message)
    await message.answer(
        _PROMPT,
        # «❌ Отмена» рядом с «✅ Готово» — как в остальных экранах ввода: выход
        # без отправки должен быть виден, а не угадываться.
        reply_markup=keyboards.yes_no_keyboard(
            yes_cb="feedback:done", no_cb="feedback:cancel",
            yes_text="✅ Готово", no_text="❌ Отмена",
        ),
    )


@router.message(StateFilter(FeedbackFlow.awaiting_message), _NOT_A_COMMAND)
async def feedback_message(message: Message, state: FSMContext):
    if config.ADMIN_ID is None:
        await message.reply("Не настроен получатель отзывов — попробуй позже.")
        return
    who = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    await message.bot.send_message(config.ADMIN_ID, f"📬 Фидбек от {who} (id {message.from_user.id}):")
    await message.copy_to(config.ADMIN_ID)
    await message.reply(
        "Спасибо, передал 🙌 Можешь написать ещё или нажать «Готово».",
        # Кнопка на самом ответе: экран с приглашением уже уехал вверх, а
        # закончить хочется там, где только что писал.
        reply_markup=keyboards.feedback_keyboard(),
    )


@router.callback_query(StateFilter(FeedbackFlow.awaiting_message), F.data == "feedback:done")
async def feedback_done(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu

    # Состояние снимает сам _show_main_menu, и снимает бережно (каркас открытой
    # тренировки остаётся). Стоявший здесь state.clear() успевал снести каркас до
    # него — и «Продолжить» в меню открывало тренировку без упражнений.
    await _show_main_menu(callback, state)
    await callback.answer("Спасибо за отзыв!")


@router.callback_query(StateFilter(FeedbackFlow.awaiting_message), F.data == "feedback:cancel")
async def feedback_cancel(callback: CallbackQuery, state: FSMContext):
    """«❌ Отмена» — выйти из отзыва. Уже отправленное этим не отзывается (обратно
    из чата админа не вынуть), так что и обещать этого не будем."""
    from handlers.workout import _show_main_menu

    await _show_main_menu(callback, state)
    await callback.answer("Вышел из отзыва")
