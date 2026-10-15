"""Мягкая остановка на деплое: дать идущим ходам AI-тренера договорить.

Fly на каждом развороте шлёт сигнал и через `kill_timeout` (fly.toml) добивает
процесс. Раньше бот гас за 0.1 с: стрим `/v1/ai/ask` отдавал приложению 500,
ответ в Telegram обрывался на полуслове, а cost-событие хода не находило
закрытую базу. Теперь порядок такой (`shutdown`):

1. Сигнал → `begin_drain()`: поллинг Telegram останавливается (новых апдейтов
   нет), новые ходы `/v1/ai/*` получают 503 с `Retry-After` (см.
   `api_v1_ai._drain_aware`).
2. `drain()` ждёт, пока счётчик идущих ходов (`track`) не обнулится, но не
   дольше `config.SHUTDOWN_AI_GRACE_SECONDS`.
3. Только потом — остановка HTTP-сервера, фоновых задач, сессии бота и
   `db.close_db()`.

Что считается ходом: хендлер вопроса тренеру в боте (`_handle_question`),
ручки `/v1`, которые зовут модель, стрим-задача `/ai/ask` (живёт дольше своего
обработчика) и любой `ai_trainer.paid_call` вне этих ходов (недельный
дайджест, разбор тренировки — фоновые платные вызовы).

Процесс один на всех, event loop один — поэтому хватает простого словаря без
замков. Текста наружу модуль не отдаёт: 503 рисует `api_v1_ai` через i18n.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional

import config

logger = logging.getLogger(__name__)

# Через сколько секунд приложению стоит повторить запрос, получив 503 на
# остановке: новая машина на Fly поднимается за 10–30 с после старой.
RETRY_AFTER_SECONDS = 30

# Сколько после дренажа даём uvicorn дописать хвосты ответов (последний
# `done` стрима, обычные запросы) перед закрытием базы. Плюс грейс — должно
# укладываться в kill_timeout из fly.toml.
HTTP_STOP_SECONDS = 5.0

_POLL_SECONDS = 0.05

_active: dict[int, str] = {}
_ids = itertools.count(1)
_draining = False
_inside_turn: contextvars.ContextVar[bool] = contextvars.ContextVar("shutdown_drain_inside", default=False)


def is_draining() -> bool:
    return _draining


def begin_drain() -> None:
    """Перестать брать новые ходы. Идемпотентно."""
    global _draining
    _draining = True


def in_flight() -> int:
    return len(_active)


def in_flight_by_kind() -> dict[str, int]:
    return dict(Counter(_active.values()))


def reset() -> None:
    """Для тестов: вернуть модуль в состояние «процесс только что стартовал»."""
    global _draining
    _draining = False
    _active.clear()


@asynccontextmanager
async def track(kind: str) -> AsyncIterator[None]:
    """Учесть один идущий ход на время блока — при любом исходе."""
    token = next(_ids)
    _active[token] = kind
    marker = _inside_turn.set(True)
    try:
        yield
    finally:
        _inside_turn.reset(marker)
        _active.pop(token, None)


@asynccontextmanager
async def track_unless_inside(kind: str) -> AsyncIterator[None]:
    """`track`, но не второй раз внутри уже учтённого хода: платный вызов
    посреди хода тренера — часть этого хода, а не ещё один."""
    if _inside_turn.get():
        yield
        return
    async with track(kind):
        yield


def tracked(kind: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Декоратор: весь вызов корутины — один ход."""

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with track(kind):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


async def drain(grace: Optional[float] = None) -> int:
    """Дождаться конца идущих ходов, но не дольше `grace` секунд.

    Возвращает, сколько ходов осталось недоделанными (0 — все договорили)."""
    if grace is None:
        grace = config.SHUTDOWN_AI_GRACE_SECONDS
    started = time.monotonic()
    count = in_flight()
    logger.warning("shutdown: draining %d AI turns, up to %.0f s %s", count, grace, in_flight_by_kind() or "")
    deadline = started + grace
    while _active and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)
    elapsed = time.monotonic() - started
    left = in_flight()
    if left:
        logger.error(
            "shutdown: grace expired after %.1f s, %d AI turns cut %s", elapsed, left, in_flight_by_kind()
        )
    else:
        logger.warning("shutdown: drained in %.1f s", elapsed)
    return left


async def shutdown(
    *,
    grace: Optional[float] = None,
    stop_http: Optional[Callable[[], Awaitable[None]]] = None,
    background: Iterable[asyncio.Task] = (),
    closers: Iterable[Callable[[], Awaitable[Any]]] = (),
    close_db: Optional[Callable[[], Awaitable[None]]] = None,
) -> int:
    """Весь хвост остановки процесса по порядку: дренаж → HTTP → фон → сессии
    → база. Каждый шаг под своим перехватом: упавший шаг не должен оставить
    базу незакрытой. Возвращает число оборванных ходов."""
    begin_drain()
    left = await drain(grace)
    if stop_http is not None:
        try:
            await stop_http()
        except Exception:
            logger.exception("shutdown: HTTP server stop failed")
    for task in background:
        task.cancel()
    for close in closers:
        try:
            await close()
        except Exception:
            logger.exception("shutdown: closer failed")
    if close_db is None:
        import db

        close_db = db.close_db
    await close_db()
    return left
