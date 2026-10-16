"""Живые цифры недели для главной сайта — «неделя в подвале».

Только настоящие числа из базы (`db.site_week_numbers`: закрытые
тренировки, подходы и тонны за 7 дней, без импорта и без своих аккаунтов).
Считаются не на каждый просмотр, а раз в CACHE_SECONDS; страница берёт
последнее посчитанное. Нет базы или числа нулевые — блока с цифрами нет
вовсе: придуманных или «примерных» цифр сайт не показывает.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)

CACHE_SECONDS = 1800
_cache: dict[str, object] = {"at": 0.0, "data": None}


async def refresh() -> Optional[dict]:
    """Пересчитать, если устарело; вернуть то, что есть."""
    now = time.monotonic()
    if _cache["at"] and now - float(_cache["at"]) < CACHE_SECONDS:  # type: ignore[arg-type]
        return _cache["data"]  # type: ignore[return-value]
    _cache["at"] = now
    try:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        data = await db.site_week_numbers(since, config.limit_preview_ids())
    except Exception:  # noqa: BLE001 — без базы страница просто без цифр
        logger.debug("site numbers unavailable", exc_info=True)
        data = None
    _cache["data"] = data if data and data.get("workouts") else None
    return _cache["data"]  # type: ignore[return-value]


def cached() -> Optional[dict]:
    return _cache["data"]  # type: ignore[return-value]


def reset() -> None:
    _cache["at"] = 0.0
    _cache["data"] = None
