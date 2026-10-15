"""Старые строки бывшего дневника еды — для тестов выгрузки, удаления и слияния.

Дневника еды больше нет: код в `food_entries` не пишет ничего (см.
api_v1_food.py). Но строки, которые люди успели записать раньше, живут в базе,
уезжают в выгрузку и стираются «Удалить аккаунт» — и это надо проверять.
Поэтому тесты кладут такую строку сами, прямым INSERT, а не через приложение.
"""

import datetime as dt

import db


async def insert_food_row(
    telegram_id: int,
    eaten_on: str,
    description: str,
    *,
    details: str | None = None,
    calories: float | None = None,
    protein: float | None = None,
    fat: float | None = None,
    carbs: float | None = None,
) -> int:
    cur = await db.conn().execute(
        "INSERT INTO food_entries (telegram_id, eaten_on, description, details, calories, "
        "protein, fat, carbs, photo_file_id, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'text', ?)",
        (
            telegram_id, eaten_on, description, details, calories, protein, fat, carbs,
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    await db.conn().commit()
    return cur.lastrowid
