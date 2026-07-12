import asyncio
import logging
from contextlib import suppress

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, Message

import admin_tasks
import config
import db
import engagement
import keyboards
from fsm_storage import JSONFileStorage
from handlers import (
    admin,
    ai_trainer,
    backfill,
    csv_import,
    edit_workout,
    exercise_resolve,
    exercises,
    fallback,
    history,
    persistent_menu,
    settings,
    workout,
)

logger = logging.getLogger(__name__)

# Substrings (Telegram's error messages, lowercased) that mean "the user's
# screen is already stale/gone" rather than a real bug — safe to swallow.
_BENIGN_BAD_REQUEST_SUBSTRINGS = (
    "query is too old",
    "query id is invalid",
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "message can't be deleted",
    "message can't be edited",
)


class IgnoreStaleCallbackMiddleware(BaseMiddleware):
    """Swallow Telegram errors for callback queries that expired before we could answer them.

    Handlers do their work (DB calls, message edits) before calling
    callback.answer(), so a slow step can leave the callback query stale by
    the time answer() runs, or the underlying message can vanish (deleted by
    the user, replaced by a newer screen, etc). Telegram then rejects the
    call; this is harmless to the user and shouldn't surface as an
    unhandled exception that leaves their tap spinner stuck forever.
    """

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            message = e.message.lower()
            if any(s in message for s in _BENIGN_BAD_REQUEST_SUBSTRINGS):
                logger.warning("Swallowed benign TelegramBadRequest: %s", e.message)
                with suppress(TelegramBadRequest):
                    await event.answer()
                return None
            raise


class RefreshPersistentMenuMiddleware(BaseMiddleware):
    """Catches every user up to the latest persistent-keyboard button set on
    their very next interaction with the bot — any text message or button
    tap — rather than only resyncing when they happen to hit /start or the
    Меню button. Runs after the handler so the normal reply goes out first.
    """

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        target = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(target, Message):
            return result
        user = await db.get_user(event.from_user.id)
        if user is None or user["reply_keyboard_version"] >= keyboards.PERSISTENT_MENU_VERSION:
            return result
        with suppress(TelegramBadRequest):
            await target.answer(
                "⌨️ Обновил меню под полем ввода.",
                reply_markup=keyboards.persistent_menu(),
            )
        await db.update_user(event.from_user.id, reply_keyboard_version=keyboards.PERSISTENT_MENU_VERSION)
        return result


async def _setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="ai_trainer", description="AI-тренер"),
        ],
        scope=BotCommandScopeDefault(),
    )
    if config.ADMIN_ID is not None:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть главное меню"),
                BotCommand(command="ai_trainer", description="AI-тренер"),
                BotCommand(command="check_users", description="Список пользователей (админ)"),
            ],
            scope=BotCommandScopeChat(chat_id=config.ADMIN_ID),
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError("TG_TOKEN env var is not set")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(disable_notification=True))
    await _setup_commands(bot)
    dp = Dispatcher(storage=JSONFileStorage(config.FSM_STORAGE_PATH))
    dp.callback_query.outer_middleware(IgnoreStaleCallbackMiddleware())
    dp.message.outer_middleware(RefreshPersistentMenuMiddleware())
    dp.callback_query.outer_middleware(RefreshPersistentMenuMiddleware())
    dp.include_router(persistent_menu.router)
    dp.include_router(workout.router)
    dp.include_router(admin.router)
    dp.include_router(backfill.router)
    dp.include_router(exercise_resolve.router)
    dp.include_router(csv_import.router)
    dp.include_router(exercises.router)
    dp.include_router(history.router)
    dp.include_router(edit_workout.router)
    dp.include_router(ai_trainer.router)
    dp.include_router(settings.router)
    dp.include_router(fallback.router)

    admin_job = asyncio.create_task(admin_tasks.run_daily_admin_jobs(bot))
    engagement_job = asyncio.create_task(engagement.run_daily_engagement_job(bot))
    try:
        await dp.start_polling(bot)
    finally:
        admin_job.cancel()
        engagement_job.cancel()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
