"""Тревоги админу в Telegram: ошибки из лога, новые сбои iOS-приложения,
часовые проверки и пульс для внешнего мониторинга.

Зачем. До этого модуля всё, что ломалось, уходило только в лог процесса:
необработанное исключение в хендлере (`main.on_unhandled_error`), 500 в REST
`/v1` (`api_v1_common.unhandled_error_handler`), умершая фоновая задача,
падения приложения из MetricKit (их было видно только по `/crashes`). Узнать
об аварии можно было, только если зайти и посмотреть — то есть никогда.

**Ошибки из лога.** `AdminAlertHandler` висит на корневом логгере и ловит всё
уровня ERROR и выше из любого модуля — новый `logger.exception(...)` в коде
становится тревогой сам, без правки здесь. Одинаковые ошибки склеиваются по
подписи (`signature`): первая уходит сразу, повторы копятся и раз в
`REPEAT_FLUSH_SECONDS` приходят одной строкой «ещё ×N». Иначе упавшая база
прислала бы по сообщению на каждый запрос. Подпись — тип исключения и
последний кадр стека, а без исключения — шаблон сообщения (`record.msg`, до
подстановки аргументов): «не доставлен пуш пользователю %s» у разных людей —
одна ошибка, а не сто.

Не шлём:
- записи самого этого модуля — сбой отправки тревоги не должен порождать
  тревогу о сбое отправки;
- записи с `extra={"ops_alert": False}` — там, где код уже сам пишет админу
  (протухший бэкап в `admin_tasks`), второе сообщение о том же — шум;
- «Failed to fetch updates» от поллинга aiogram — это моргнувшая связь с
  Telegram, поллинг сам переподключается, а если Telegram лёг надолго, то и
  тревогу доставить некуда. Процесс, который жив, но совсем не видит
  Telegram, ловит внешний мониторинг (см. «Пульс» ниже).

Лог-хендлер синхронный и может позваться из чужого потока (`to_thread`,
`run_in_executor`), поэтому он только кладёт готовый текст в очередь через
`call_soon_threadsafe`, а шлёт `run_alert_sender` на основном цикле.

**Новые сбои iOS.** `api_v1_diagnostics` зовёт `crash_alert_text` на каждый
пришедший отчёт; тревога — только если такого же сбоя (вид, версия, сборка,
тип исключения, сигнал) в базе ещё не было. Повторы видны в `/crashes`.

**Часовые проверки** (`run_hourly_checks`): суточный расход AI дошёл до
мягкого или жёсткого потолка (`ai_limits.spend_level`) — раз на ступень за
сутки; большинство пушей APNs за час не доставлено (ключ отозван, сменился
bundle id) — это `logger.warning`, в тревоги из лога оно не попадает.

**Пульс.** Мёртвый процесс сам ничего не пришлёт. Если задан
`config.HEALTHCHECK_PING_URL` (например, чек Healthchecks.io с Telegram-
интеграцией), `run_heartbeat` дёргает его раз в `HEARTBEAT_SECONDS`; перестал
— сервис сам напишет, что бот молчит. Инструкция — `docs/ALERTS.md`.

Всё выключается без `ADMIN_ID` (некому слать) или `OPS_ALERTS_ENABLED=false`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

# Повторы одной ошибки копятся и приходят сводкой не чаще этого.
REPEAT_FLUSH_SECONDS = 15 * 60
# Ошибка, которой не было столько времени, снова считается новой и уходит
# сразу, а не строчкой в сводке: вернувшаяся через полдня поломка — новость.
QUIET_RESET_SECONDS = 6 * 3600
# Сколько разных новых ошибок уходит отдельными сообщениями за одно окно
# сводки. Остальные — строчками в сводке: при каскадном сбое десяток
# сообщений ещё читается, сотня — нет.
MAX_IMMEDIATE_PER_WINDOW = 10
# Хвост стека в тревоге. Целиком он в логе, а в чате нужно место, где упало.
TRACEBACK_TAIL_LINES = 12
# Потолок Telegram — 4096 символов; берём с запасом под разметку.
MAX_MESSAGE_CHARS = 3500

HOURLY_CHECK_SECONDS = 3600
HEARTBEAT_SECONDS = 5 * 60
# APNs: тревога, если за час попыток не меньше этого и не доставлена хотя бы
# половина. Одиночные отказы (человек выключил уведомления) — не авария.
APNS_MIN_ATTEMPTS = 5
APNS_FAIL_SHARE = 0.5

_IGNORED_MESSAGE_PREFIXES = ("Failed to fetch updates",)


def enabled() -> bool:
    return config.ADMIN_ID is not None and config.OPS_ALERTS_ENABLED


# --- склейка повторов --------------------------------------------------------


@dataclass
class _Seen:
    first_line: str
    last_at: float
    pending: int = 0


@dataclass
class Aggregator:
    """Решает, уходит ли ошибка сразу или копится в сводку. Чистая логика со
    временем снаружи — чтобы тест не ждал пятнадцать минут."""

    seen: dict[str, _Seen] = field(default_factory=dict)
    window_started: float = 0.0
    immediate_in_window: int = 0

    def observe(self, sig: str, first_line: str, now: float) -> bool:
        """True — отправить сейчас. False — посчитано в сводку."""
        entry = self.seen.get(sig)
        if entry is not None and now - entry.last_at < QUIET_RESET_SECONDS:
            entry.last_at = now
            entry.pending += 1
            return False
        if now - self.window_started >= REPEAT_FLUSH_SECONDS:
            self.window_started = now
            self.immediate_in_window = 0
        if self.immediate_in_window >= MAX_IMMEDIATE_PER_WINDOW:
            # Не отправили ни разу — в сводке это «ещё ×N» от нуля, и первое
            # вхождение тоже надо посчитать.
            self.seen[sig] = _Seen(first_line, now, pending=1)
            return False
        self.immediate_in_window += 1
        self.seen[sig] = _Seen(first_line, now)
        return True

    def take_repeats(self) -> list[tuple[str, int]]:
        """(первая строка ошибки, сколько раз с прошлой сводки) — и обнулить."""
        out = []
        for entry in self.seen.values():
            if entry.pending:
                out.append((entry.first_line, entry.pending))
                entry.pending = 0
        return out


def repeats_text(repeats: list[tuple[str, int]]) -> str:
    lines = [f"🔁 <b>Повторялось за {REPEAT_FLUSH_SECONDS // 60} мин.</b>"]
    for first_line, n in sorted(repeats, key=lambda r: -r[1]):
        lines.append(f"×{n} — <code>{escape(first_line[:200])}</code>")
    return _clip("\n".join(lines))


# --- лог-записи → текст --------------------------------------------------------


def signature(record: logging.LogRecord) -> str:
    exc = record.exc_info[1] if record.exc_info else None
    if exc is not None:
        tb = traceback.extract_tb(exc.__traceback__)
        where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else ""
        return f"{type(exc).__name__}@{where}"
    return f"{record.name}:{record.msg}"


def _first_line(record: logging.LogRecord) -> str:
    exc = record.exc_info[1] if record.exc_info else None
    text = f"{type(exc).__name__}: {exc}" if exc is not None else record.getMessage()
    return (text.splitlines() or [record.name])[0]


def record_text(record: logging.LogRecord) -> str:
    try:
        message = record.getMessage()
    except Exception:
        message = str(record.msg)
    lines = [
        f"🚨 <b>{escape(record.levelname)}</b> · <code>{escape(record.name)}</code>",
        escape(message[:600]),
    ]
    if record.exc_info and record.exc_info[1] is not None:
        tb = "".join(traceback.format_exception(*record.exc_info)).rstrip().splitlines()
        tail = "\n".join(tb[-TRACEBACK_TAIL_LINES:])
        lines.append(f"<pre>{escape(tail)}</pre>")
    return _clip("\n".join(lines))


def _clip(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    # Не режем посреди тега: закрываем <pre>, если он был открыт.
    cut = text[:MAX_MESSAGE_CHARS]
    if cut.count("<pre>") > cut.count("</pre>"):
        cut += "\n…</pre>"
    else:
        cut += "\n…"
    return cut


def should_alert(record: logging.LogRecord) -> bool:
    if record.levelno < logging.ERROR:
        return False
    if record.name == __name__ or record.name.startswith(__name__ + "."):
        return False
    if getattr(record, "ops_alert", True) is False:
        return False
    msg = record.msg if isinstance(record.msg, str) else ""
    return not msg.startswith(_IGNORED_MESSAGE_PREFIXES)


# --- очередь и отправка --------------------------------------------------------

_loop: Optional[asyncio.AbstractEventLoop] = None
_queue: Optional[asyncio.Queue] = None
_aggregator = Aggregator()


class AdminAlertHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not should_alert(record):
                return
            enqueue_error(signature(record), _first_line(record), record_text(record))
        except Exception:
            # Лог-хендлер не имеет права бросать: упадёт тот, кто логировал.
            pass


def enqueue_error(sig: str, first_line: str, text: str) -> None:
    """Потокобезопасно: склейка и очередь трогаются только на основном цикле."""
    loop = _loop
    if loop is None or loop.is_closed():
        return

    def _put() -> None:
        if _queue is None:
            return
        if _aggregator.observe(sig, first_line, time.monotonic()):
            _queue.put_nowait(text)

    loop.call_soon_threadsafe(_put)


def enqueue_text(text: str) -> None:
    """Готовая тревога в обход склейки (новый сбой iOS, часовые проверки)."""
    loop = _loop
    if loop is None or loop.is_closed() or _queue is None:
        return
    loop.call_soon_threadsafe(_queue.put_nowait, _clip(text))


def install() -> Optional[AdminAlertHandler]:
    """Повесить хендлер на корневой логгер. Звать с работающего цикла."""
    global _loop, _queue
    if not enabled():
        return None
    _loop = asyncio.get_running_loop()
    _queue = asyncio.Queue()
    handler = AdminAlertHandler()
    logging.getLogger().addHandler(handler)
    return handler


async def _send(bot: Any, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=config.ADMIN_ID, text=text, parse_mode="HTML", disable_notification=False
        )
    except Exception as exc:
        # warning, не error: до своего же хендлера не доходит, но в логе видно.
        logger.warning("ops alert not delivered: %s: %s", type(exc).__name__, exc)


async def run_alert_sender(bot: Any) -> None:
    if _queue is None:
        return
    next_flush = time.monotonic() + REPEAT_FLUSH_SECONDS
    while True:
        timeout = max(0.0, next_flush - time.monotonic())
        try:
            text = await asyncio.wait_for(_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            text = None
        if text is not None:
            await _send(bot, text)
        if time.monotonic() >= next_flush:
            next_flush = time.monotonic() + REPEAT_FLUSH_SECONDS
            repeats = _aggregator.take_repeats()
            if repeats:
                await _send(bot, repeats_text(repeats))


# --- новые сбои iOS ------------------------------------------------------------

_CRASH_KIND_LABELS = {
    "crash": "Падение",
    "hang": "Зависание",
    "cpu_exception": "Перерасход CPU",
    "disk_write_exception": "Запись на диск",
    "keychain_save_failed": "Keychain не сохранил вход",
}


def crash_alert_text(
    *,
    diagnostic_id: int,
    kind: str,
    app_version: Optional[str],
    build: Optional[str],
    os_version: Optional[str],
    device: Optional[str],
    user_id: Optional[int],
    meta: dict,
) -> str:
    reason = []
    for key, label in (
        ("exceptionType", "exc"),
        ("exceptionCode", "code"),
        ("signal", "sig"),
    ):
        if meta.get(key) is not None:
            reason.append(f"{label} {meta[key]}")
    termination = meta.get("terminationReason")
    who = f"id {user_id}" if user_id is not None else "без входа"
    lines = [
        f"🧯 <b>{escape(_CRASH_KIND_LABELS.get(kind, kind))} iOS — новое в этой сборке</b>",
        f"{escape(str(app_version))} ({escape(str(build))}) · {escape(str(device))} "
        f"iOS {escape(str(os_version))} · {who}",
    ]
    if reason:
        lines.append(escape(", ".join(reason)))
    if termination:
        lines.append(escape(str(termination)[:300]))
    lines.append(f"Стек: <code>SELECT payload FROM diagnostics WHERE id = {diagnostic_id}</code>")
    return "\n".join(lines)


async def maybe_alert_new_diagnostic(
    *,
    diagnostic_id: int,
    kind: str,
    app_version: Optional[str],
    build: Optional[str],
    os_version: Optional[str],
    device: Optional[str],
    user_id: Optional[int],
    meta: dict,
) -> bool:
    """Тревога, если такого сбоя в этой сборке ещё не было. Не бросает."""
    if not enabled() or kind not in ("crash", "hang", "keychain_save_failed"):
        return False
    import db

    try:
        seen = await db.diagnostic_seen_before(
            diagnostic_id=diagnostic_id,
            kind=kind,
            app_version=app_version,
            build=build,
            exception_type=meta.get("exceptionType"),
            signal=meta.get("signal"),
        )
    except Exception:
        logger.warning("ops alert: diagnostic lookup failed", exc_info=True)
        return False
    if seen:
        return False
    enqueue_text(
        crash_alert_text(
            diagnostic_id=diagnostic_id, kind=kind, app_version=app_version, build=build,
            os_version=os_version, device=device, user_id=user_id, meta=meta,
        )
    )
    return True


# --- часовые проверки ----------------------------------------------------------

_apns_attempts = 0
_apns_failures = 0
_spend_alerted: dict[str, str] = {}  # ступень → сутки UTC, за которые уже сказали


def record_apns_result(delivered: bool) -> None:
    global _apns_attempts, _apns_failures
    _apns_attempts += 1
    if not delivered:
        _apns_failures += 1


def take_apns_counts() -> tuple[int, int]:
    global _apns_attempts, _apns_failures
    counts = (_apns_attempts, _apns_failures)
    _apns_attempts = _apns_failures = 0
    return counts


def apns_alert_text(attempts: int, failures: int) -> Optional[str]:
    if attempts < APNS_MIN_ATTEMPTS or failures < attempts * APNS_FAIL_SHARE:
        return None
    return (
        f"📵 <b>Пуши iOS не доходят</b>: за час не доставлено {failures} из {attempts}.\n"
        "Причины в логе — «APNs: пуш пользователю … не доставлен, HTTP …» "
        "(ключ, bundle id, sandbox/production)."
    )


def spend_alert_text(level: Optional[str], spend: float, day: str) -> Optional[str]:
    import ai_limits

    if level is None or _spend_alerted.get(level) == day:
        return None
    _spend_alerted[level] = day
    if level == ai_limits.KIND_SPEND_HARD:
        return (
            f"💸 <b>AI замолчал</b>: расход за сутки ~${spend:.2f} дошёл до жёсткого потолка "
            f"(${config.AI_DAILY_COST_HARD_STOP_USD:g}). Тренер молчит до полуночи UTC."
        )
    return (
        f"💸 <b>AI на мягком потолке</b>: расход за сутки ~${spend:.2f} "
        f"(порог ${config.AI_DAILY_COST_SOFT_CAP_USD:g}) — дорогое выключено до полуночи UTC."
    )


async def run_hourly_checks() -> None:
    import ai_limits
    import db

    while True:
        await asyncio.sleep(HOURLY_CHECK_SECONDS)
        try:
            attempts, failures = take_apns_counts()
            text = apns_alert_text(attempts, failures)
            if text:
                enqueue_text(text)
            level = await ai_limits.spend_level()
            text = spend_alert_text(level, await ai_limits.daily_spend_usd(), db._utc_day())
            if text:
                enqueue_text(text)
        except Exception:
            logger.warning("ops hourly check failed", exc_info=True)


# --- пульс для внешнего мониторинга ---------------------------------------------


async def run_heartbeat() -> None:
    url = config.HEALTHCHECK_PING_URL
    if not url:
        return
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            with suppress(Exception):
                await client.get(url)
            await asyncio.sleep(HEARTBEAT_SECONDS)
