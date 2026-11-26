"""Толпа атлетов для «Места в зале» (crowd_standing): сырыми INSERT — быстро,
по одной тренировке и одному подходу на атлета, без API."""

from __future__ import annotations

import datetime as dt
from typing import Optional


def _iso(days_ago: float = 3) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


async def athlete(
    db, user_id: int, identity: Optional[str], weight: float, reps: int = 5, *, gender: Optional[str] = None,
    unit: str = "kg", bodyweight: Optional[float] = None, lang: str = "ru", name: Optional[str] = None,
    days_ago: float = 3, load_weight: Optional[float] = None,
) -> int:
    """Атлет с одним подходом в закрытой тренировке; вернёт id упражнения.
    `identity` None — своё упражнение без каталога (`name`)."""
    c = db.conn()
    iso = _iso(days_ago)
    await c.execute(
        "INSERT OR IGNORE INTO users (telegram_id, created_at, unit, gender, lang) VALUES (?, ?, ?, ?, ?)",
        (user_id, iso, unit, gender, lang),
    )
    shown = name or identity
    cur = await c.execute(
        "INSERT INTO exercises (user_id, name, display_name, original_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, shown, shown, identity, iso),
    )
    ex_id = cur.lastrowid
    if bodyweight:
        await c.execute("INSERT INTO bodyweight_logs (telegram_id, weight, logged_at) VALUES (?, ?, ?)",
                        (user_id, bodyweight, iso))
    cur = await c.execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status) VALUES (?, ?, ?, 'finished')",
        (user_id, iso, iso),
    )
    cur = await c.execute("INSERT INTO workout_blocks (workout_id, order_index) VALUES (?, 0)", (cur.lastrowid,))
    await c.execute(
        "INSERT INTO sets (block_id, exercise_id, round_index, weight, load_weight, reps, created_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?)",
        (cur.lastrowid, ex_id, weight, load_weight, reps, iso),
    )
    await c.commit()
    return ex_id


async def crowd(db, identity: str, weights: list[float], *, start: int = 50000, reps: int = 5, **kw) -> None:
    for i, w in enumerate(weights):
        await athlete(db, start + i, identity, w, reps, **kw)
