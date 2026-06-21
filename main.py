import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import admin_tasks
import config
import db
from handlers import (
    backfill,
    bodyweight,
    csv_import,
    edit_workout,
    exercise_resolve,
    exercises,
    history,
    settings,
    workout,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError("TG_TOKEN env var is not set")

    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(workout.router)
    dp.include_router(backfill.router)
    dp.include_router(exercise_resolve.router)
    dp.include_router(csv_import.router)
    dp.include_router(exercises.router)
    dp.include_router(history.router)
    dp.include_router(edit_workout.router)
    dp.include_router(settings.router)
    dp.include_router(bodyweight.router)

    admin_job = asyncio.create_task(admin_tasks.run_daily_admin_jobs(bot))
    try:
        await dp.start_polling(bot)
    finally:
        admin_job.cancel()
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
