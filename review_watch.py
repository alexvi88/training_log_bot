"""Зеркало действий Apple-ревьюера админу в личку.

Зачем. Когда Beta App Review заходит в приложение под демо-аккаунтом
(review_demo.py), владельцу важно видеть, что ревьюер делает и когда: началось
ли ревью, куда он нажал, на чём споткнулся. Лента `user_events` это уже знает —
сюда пишут и бот (source='tg'), и REST `/v1` (source='ios'), — поэтому второго
журнала нет: `db.log_user_event` после успешной записи зовёт `on_event`, а тот
решает, наш ли это аккаунт, и кладёт строку в очередь.

Какой аккаунт ревьюерский. Строго по `auth_identities`: провайдер
`review_demo` и логин `config.REVIEW_DEMO_USERNAME` (так же его находит
review_demo.ensure_demo_user). Ни имя, ни почта, ни язык в этом не участвуют, и
у настоящего человека такой привязки не бывает. Дополнительно в базу ходим
только за отрицательными id (app-only аккаунты): у людей из Telegram id
положительный, и для них модуль не делает ничего. Второй демо-аккаунт
(`WALK_DEMO_*`, прогон скриншотов владельца) не зеркалится: его привязка — тот
же провайдер, но другой логин.

Если ревьюер удалит аккаунт, при следующем входе заведётся новый с другим id:
кэш (60 с) сбрасывается и в ensure_demo_user, и при удалении аккаунта
(account_deletion), так что зеркало не молчит и не шлёт чужого.

Отправка не в горячем пути. `on_event` только кладёт строку в очередь; фоновая
задача (стартует лениво на первом событии) склеивает события за пару секунд в
одно сообщение, режет по лимиту Telegram и шлёт не чаще раза в секунду. Сбой
отправки глотается и пишется в лог — запись события от него не зависит. Тихо
(`disable_notification`) всё, кроме первого события новой сессии (пауза больше
30 минут): это сигнал «ревью началось».

Всё выключается без `ADMIN_ID`, без логина ревьюера или флагом
`REVIEW_WATCH_ENABLED=false`; выключенный модуль не ходит ни в сеть, ни в базу.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import config

logger = logging.getLogger(__name__)

PROVIDER = "review_demo"  # тот же, что review_demo.PROVIDER (не импортируем: цикл)

HEADER = "🍎 ревьюер"
CACHE_TTL_SECONDS = 60.0
SESSION_GAP_SECONDS = 30 * 60
COALESCE_SECONDS = 2.0
MIN_SEND_INTERVAL_SECONDS = 1.0
# Потолок Telegram — 4096; запас под шапку.
MAX_MESSAGE_CHARS = 4000
MAX_QUEUE = 500
LINE_LIMIT = 300
AI_REPLY_LIMIT = 200


@dataclass
class _Item:
    at: dt.datetime  # наивный UTC, как в базе
    source: str
    kind: str
    content: str
    loud: bool = False


Sender = Callable[[str, bool], Awaitable[None]]  # (text, silent)

_queue: list[_Item] = []
_dropped = 0
_cache: Optional[tuple[Optional[int], float]] = None  # (user_id ревьюера, до какого monotonic)
_last_event_mono: Optional[float] = None
_task: Optional[asyncio.Task] = None
_wakeup: Optional[asyncio.Event] = None
_bot: Any = None
_sender: Optional[Sender] = None


def enabled() -> bool:
    return bool(config.REVIEW_WATCH_ENABLED and config.ADMIN_ID is not None and config.REVIEW_DEMO_USERNAME)


def set_bot(bot: Any) -> None:
    """Общий Bot процесса (main.py). Без него отправка берёт короткоживущий."""
    global _bot
    _bot = bot


def invalidate_cache() -> None:
    """Аккаунт ревьюера пересоздан или удалён — id в кэше больше не верен."""
    global _cache
    _cache = None


def reset() -> None:
    """Для тестов: всё состояние живёт в памяти процесса."""
    global _queue, _dropped, _cache, _last_event_mono, _task, _wakeup, _bot, _sender
    if _task is not None and not _task.done():
        with contextlib.suppress(RuntimeError):
            _task.cancel()
    _queue, _dropped, _cache, _last_event_mono = [], 0, None, None
    _task = _wakeup = _bot = _sender = None


async def _reviewer_user_id() -> Optional[int]:
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[1] > now:
        return _cache[0]
    import db  # локально: db тянет этот модуль из log_user_event

    uid = await db.resolve_auth_identity(PROVIDER, config.REVIEW_DEMO_USERNAME.strip())
    _cache = (uid, now + CACHE_TTL_SECONDS)
    return uid


def _one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_line(item: _Item) -> str:
    """`HH:MM:SS ios · вид · содержимое`. Время — на часах админа. Только
    вид и содержимое события: payload (callback_data и т.п.) не показываем."""
    local = item.at + dt.timedelta(hours=config.ADMIN_TZ_OFFSET)
    limit = AI_REPLY_LIMIT if item.kind == "ai_reply" else LINE_LIMIT
    return f"{local.strftime('%H:%M:%S')} {item.source} · {item.kind} · {_one_line(item.content, limit)}"


def build_messages(lines: list[str], dropped: int = 0) -> list[str]:
    """Строки → сообщения до MAX_MESSAGE_CHARS, у каждого шапка."""
    if dropped:
        lines = [f"… пропущено событий: {dropped}", *lines]
    messages: list[str] = []
    current = HEADER
    for line in lines:
        if len(current) + 1 + len(line) > MAX_MESSAGE_CHARS and current != HEADER:
            messages.append(current)
            current = HEADER
        current += "\n" + line[: MAX_MESSAGE_CHARS - len(HEADER) - 1]
    if current != HEADER:
        messages.append(current)
    return messages


async def on_event(telegram_id: int, kind: str, content: str, source: str) -> None:
    """Зовётся из db.log_user_event ПОСЛЕ записи. Никогда не бросает."""
    try:
        # Живые люди из Telegram — положительные id; не ходим в базу вовсе.
        if telegram_id >= 0 or not enabled():
            return
        if await _reviewer_user_id() != telegram_id:
            return
        _enqueue(_Item(dt.datetime.utcnow(), source, kind, content))
    except Exception:
        logger.warning("review_watch: on_event failed", exc_info=True)


def _enqueue(item: _Item) -> None:
    global _dropped, _last_event_mono
    now = time.monotonic()
    item.loud = _last_event_mono is None or now - _last_event_mono > SESSION_GAP_SECONDS
    _last_event_mono = now
    _queue.append(item)
    while len(_queue) > MAX_QUEUE:
        _queue.pop(0)
        _dropped += 1
    _ensure_task()
    if _wakeup is not None:
        _wakeup.set()


def _ensure_task() -> None:
    global _task, _wakeup
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done() and _task.get_loop() is loop:
        return
    _wakeup = asyncio.Event()
    _task = loop.create_task(_run(), name="review_watch")


async def _default_send(text: str, silent: bool) -> None:
    if _bot is not None:
        await _bot.send_message(config.ADMIN_ID, text, disable_notification=silent, parse_mode=None)
        return
    from aiogram import Bot  # как api_v1_feedback: на одну отправку, сессия закрывается

    bot = Bot(token=config.BOT_TOKEN)
    try:
        await bot.send_message(config.ADMIN_ID, text, disable_notification=silent, parse_mode=None)
    finally:
        await bot.session.close()


async def flush(sender: Optional[Sender] = None, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> int:
    """Отправить накопленное. Возвращает число отправленных сообщений."""
    global _queue, _dropped
    if not _queue:
        return 0
    batch, dropped = _queue, _dropped
    _queue, _dropped = [], 0
    send = sender or _sender or _default_send
    # Громкое событие шлём отдельным первым сообщением, остальное — тихо.
    groups: list[tuple[list[str], bool]] = []
    loud = [i for i in batch if i.loud]
    quiet = [i for i in batch if not i.loud]
    if loud:
        groups.append(([format_line(i) for i in loud], False))
    if quiet:
        groups.append(([format_line(i) for i in quiet], True))
    sent = 0
    for lines, silent in groups:
        for text in build_messages(lines, dropped if silent or not quiet else 0):
            if sent:
                await sleep(MIN_SEND_INTERVAL_SECONDS)
            try:
                await send(text, silent)
            except Exception as exc:
                logger.warning("review_watch: send failed: %s: %s", type(exc).__name__, exc)
            sent += 1
    return sent


async def _run() -> None:
    last_send = 0.0
    while True:
        if not _queue:
            assert _wakeup is not None
            _wakeup.clear()
            await _wakeup.wait()
        await asyncio.sleep(COALESCE_SECONDS)
        wait = MIN_SEND_INTERVAL_SECONDS - (time.monotonic() - last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await flush()
        except Exception:
            logger.warning("review_watch: flush failed", exc_info=True)
        last_send = time.monotonic()
