"""/feedback — free-form feedback (text, photos, whatever) relayed straight to the admin."""

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards
from fsm import FeedbackFlow

router = Router(name="feedback")


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FeedbackFlow.awaiting_message)
    await message.answer(
        "Напиши что угодно — отзыв, баг, идею. Можно с фото или скриншотом, можно "
        "несколькими сообщениями подряд. Передам всё админу как есть.",
        reply_markup=keyboards.feedback_keyboard(),
    )


@router.message(StateFilter(FeedbackFlow.awaiting_message))
async def feedback_message(message: Message, state: FSMContext):
    if config.ADMIN_ID is None:
        await message.reply("Не настроен получатель отзывов — попробуй позже.")
        return
    who = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    await message.bot.send_message(config.ADMIN_ID, f"📬 Фидбек от {who} (id {message.from_user.id}):")
    await message.copy_to(config.ADMIN_ID)
    await message.reply("Спасибо, передал 🙌 Можешь написать ещё или нажать «Готово».")


@router.callback_query(StateFilter(FeedbackFlow.awaiting_message), F.data == "feedback:done")
async def feedback_done(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu

    await state.clear()
    await _show_main_menu(callback, state)
    await callback.answer("Спасибо за отзыв!")
