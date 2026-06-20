import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import config
import db
from handlers import exercises, history, settings, workout


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError("TG_TOKEN env var is not set")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(workout.router)
    dp.include_router(exercises.router)
    dp.include_router(history.router)
    dp.include_router(settings.router)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
