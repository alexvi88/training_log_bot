"""Persistent reply-keyboard buttons shown under the input field.

Registered before every other router in main.py so pressing one of these
buttons always short-circuits whatever FSM state the user is currently in —
same hard reset semantics as /start.
"""

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import ai_trainer
import db
import i18n
import keyboards
from fsm import AITrainerFlow

# `INTRO_TEXT`/`RESUME_TEXT` НЕ импортируются по имени на верхнем уровне
# нарочно (см. комментарий над `_MODULE_ATTR_FUNCS` в handlers/ai_trainer.py):
# такой импорт выполняется ровно один раз при первом импорте процесса и
# замораживает язык навсегда на дефолте. Модуль ai_trainer_handlers читается
# целиком и атрибуты берутся в момент использования — тогда PEP 562
# `__getattr__` того модуля пересчитывает текст в языке ТЕКУЩЕГО запроса.
from handlers import ai_trainer as ai_trainer_handlers
from handlers.ai_trainer import ai_keyboard, intro_presets

router = Router(name="persistent_menu")


async def attach_silently(message: Message, user_id: int) -> None:
    """Give a brand-new user the keyboard under the input field without the
    "⌨️ Обновил меню" notice RefreshPersistentMenuMiddleware sends.

    That notice makes sense for an existing user whose button row just changed;
    on a first /start it's a second message about an update that never
    happened — noise on the one screen that has to sell the bot.

    A reply keyboard can only arrive attached to a message, and every screen
    here already carries an inline keyboard instead, so there's nothing to ride
    along with: the carrier is a standalone message. It used to be deleted
    right after sending on the assumption that Telegram keeps the keyboard
    once it's set — true on iOS, but live reports from Android showed the
    persistent row vanishing entirely after the carrier that attached it was
    deleted. One extra line in the chat is a small, permanent cost next to a
    client where the main navigation silently doesn't exist — and since it's
    permanent, it earns its place by naming what the row underneath actually
    does, instead of sitting there as a bare "⌨️".

    Bumps reply_keyboard_version so the middleware sees an up-to-date user and
    stays quiet. Важно, КОГДА это вызывать: RefreshPersistentMenuMiddleware
    работает ДО хендлера, поэтому гасит уведомление только та версия, которая
    поднята к моменту СЛЕДУЮЩЕГО апдейта. Отложить вызов на один тап (например,
    за кнопку в онбординге) — значит получить «⌨️ Обновил меню» на этом тапе,
    ровно там, где его быть не должно.

    Читает input_hint_hidden из базы сама, а не берёт булев параметр: не все
    вызывающие (например, settings_language_set — смена языка УЖЕ заведённым
    атлетом) держат под рукой свежую строку пользователя, а версия карусели
    не должна вернуть подсказку тому, кто её уже перерос (см. keyboards.
    persistent_menu).
    """
    user = await db.get_user(user_id)
    show_hint = not (user and user["input_hint_hidden"])
    with suppress(TelegramBadRequest):
        # Подставляем те же три подписи, что и на самой клавиатуре
        # (keyboards.BTN_WORKOUT/BTN_MENU/BTN_AI), а не переписываем их здесь
        # текстом — иначе подсказка и кнопки разойдутся при следующей правке
        # словаря кнопок.
        await message.answer(
            i18n.t(
                "persistent_menu.attach_hint",
                workout=i18n.t("btn.persistent.workout"),
                menu=i18n.t("btn.persistent.menu"),
                ai=i18n.t("btn.persistent.ai"),
            ),
            reply_markup=keyboards.persistent_menu(show_hint=show_hint),
        )
    await db.update_user(user_id, reply_keyboard_version=keyboards.PERSISTENT_MENU_VERSION)


class _MessageAsCallback:
    """Adapts a Message to the (.message, .from_user, .bot) shape that
    workout.py's screen builders expect from a CallbackQuery, so the
    persistent-keyboard handler can reuse them without forking the flow.
    """

    def __init__(self, message: Message):
        self.message = message
        self.from_user = message.from_user
        self.bot = message.bot

    async def answer(self, *args, **kwargs) -> None:
        """No-op: there's no real callback query to acknowledge here."""


@router.message(F.text == keyboards.BTN_MENU)
async def persistent_menu_button(message: Message, state: FSMContext) -> None:
    from handlers.workout import cmd_start

    await cmd_start(message, state)


@router.message(F.text == keyboards.BTN_WORKOUT)
async def persistent_workout_button(message: Message, state: FSMContext) -> None:
    from handlers.workout import _clear_state_keep_workout, start_workout

    await _clear_state_keep_workout(state)
    await start_workout(_MessageAsCallback(message), state)


async def _open_ai_trainer(message: Message, state: FSMContext) -> None:
    from handlers.workout import _clear_state_keep_workout

    if not ai_trainer.is_configured():
        await message.answer(i18n.t("persistent_menu.ai_not_configured"))
        return
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    # _clear_state_keep_workout preserves ai_history, so tapping "AI-тренер"
    # after a detour resumes the conversation rather than starting over.
    await _clear_state_keep_workout(state)
    await state.set_state(AITrainerFlow.chatting)
    data = await state.get_data()
    fresh = not data.get("ai_history")
    text = ai_trainer_handlers.INTRO_TEXT if fresh else ai_trainer_handlers.RESUME_TEXT
    # Готовые вопросы — те же, что на инлайн-входе (menu:ai), и на любом
    # заходе, свежем или нет: это отдельный экран входа, а не вклинивание в
    # ответ. Без них нижняя кнопка показывала интро, которое обещает «начни с
    # готового вопроса на кнопках ниже», а под текстом было одно «Меню»: экран
    # врал сам себе, и зависело это от того, каким из двух входов человек вошёл.
    await message.answer(
        text,
        reply_markup=await ai_keyboard(
            message.from_user.id,
            presets=await intro_presets(message.from_user.id),
        ),
        parse_mode="HTML",
    )


@router.message(F.text == keyboards.BTN_AI)
async def persistent_ai_button(message: Message, state: FSMContext) -> None:
    await _open_ai_trainer(message, state)


@router.message(Command("ai_trainer"))
async def cmd_ai_trainer(message: Message, state: FSMContext) -> None:
    await _open_ai_trainer(message, state)
