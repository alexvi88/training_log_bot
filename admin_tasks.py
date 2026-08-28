"""Daily admin job: usage stats + a DB backup, sent to ADMIN_ID via Telegram."""

import asyncio
import datetime as dt
import logging
import os
import tempfile
from contextlib import suppress
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile

import activity_log
import ai_trainer
import announcements
import config
import db
import formatting

logger = logging.getLogger(__name__)

_BACKUP_PREFIX = "training_log_backup_"

# Daily-rotation pushes are pure history past this many days; kept out of
# config.py deliberately narrow (only this job reads it) — see
# db.prune_old_pushes for why announcement categories are exempt below.
PUSH_RETENTION_DAYS = 90


def _seconds_until_next_run(hour: int) -> float:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def _llm_cost(llm_breakdown: dict[str, dict[str, int]]) -> tuple[float, int, int]:
    total_cost = 0.0
    total_calls = 0
    total_tokens = 0
    for model, stats in llm_breakdown.items():
        # Та же формула, что и в строке лога на каждый вызов (см.
        # config.call_price_usd): суточная сумма и цена запроса не должны
        # расходиться из-за двух копий арифметики.
        total_cost += config.call_price_usd(
            model,
            stats["prompt_tokens"],
            stats["completion_tokens"],
            stats.get("cached_tokens", 0),
            stats.get("reasoning_tokens", 0),
        )
        total_calls += stats["calls"]
        total_tokens += stats["prompt_tokens"] + stats["completion_tokens"]
    return total_cost, total_calls, total_tokens


async def _build_cost_report(date_str: str) -> str:
    """LLM cost breakdown for the given calendar day — real per-call token usage
    (db.cost_events, logged from ai_trainer.py) priced against
    config.LLM_PRICES_USD_PER_1K, same pattern as github.com/alexvi88/fun_bot's
    analytics.build_report."""
    llm_breakdown = await db.get_llm_cost_breakdown(date_str)
    transcriptions = await db.get_transcription_count(date_str)
    server_tools = await db.get_server_tool_count(date_str)

    llm_cost, llm_calls, llm_tokens = _llm_cost(llm_breakdown)
    transcription_cost = transcriptions * config.TRANSCRIPTION_PRICE_USD_PER_CALL
    # Вызовы web_search/x_search: $5 за 1000 СВЕРХ токенов. В консоли за неделю это
    # было $0.68 — пятнадцать процентов текстового счёта, которых отчёт не видел.
    server_tool_calls = sum(server_tools.values())
    server_tool_cost = server_tool_calls * config.SERVER_TOOL_PRICE_USD_PER_CALL
    total_cost = llm_cost + transcription_cost + server_tool_cost

    lines = [
        "",
        "🤖 AI-тренер",
        f"LLM-вызовов: {llm_calls} (~${llm_cost:.2f}, {llm_tokens:,} ток.)".replace(",", " "),
    ]
    for model, stats in sorted(llm_breakdown.items(), key=lambda x: -x[1]["calls"]):
        tok = stats["prompt_tokens"] + stats["completion_tokens"]
        lines.append(f"  └ {model}: {stats['calls']} ({tok:,} ток.)".replace(",", " "))
    if transcriptions:
        lines.append(f"Голосовых распознано: {transcriptions} (~${transcription_cost:.2f})")
    if server_tool_calls:
        lines.append(f"Поиск в сети: {server_tool_calls} вызовов (~${server_tool_cost:.2f})")
        for tool, calls in sorted(server_tools.items(), key=lambda x: -x[1]):
            lines.append(f"  └ {tool}: {calls}")
    # Отдельная строка только когда потолок реально сработал: иначе «дорогие
    # сутки, в которых людям молча отказывали в свежести» выглядят в отчёте ровно
    # как обычные. Вызовов инструментов для этого не хватает — их число зависит от
    # того, сколько запросов сделала модель внутри одного поиска.
    searches = await db.get_ai_search_count_global(date_str)
    if searches >= config.AI_SEARCH_GLOBAL_DAILY_LIMIT:
        lines.append(
            f"⚠️ Общий потолок поисков исчерпан: {searches} из "
            f"{config.AI_SEARCH_GLOBAL_DAILY_LIMIT} — дальше отвечали без свежести"
        )
    lines.append(f"💸 Итого расходы: ~${total_cost:.2f} (~${total_cost * 30:.0f}/мес)")
    # Сутки, упёршиеся в потолок по деньгам, обязаны быть видны в отчёте отдельной
    # строкой: иначе «дорогой день, в котором половина функций молча выключилась»
    # выглядит ровно как обычный, только с суммой побольше.
    if config.AI_DAILY_COST_HARD_STOP_USD > 0 and total_cost >= config.AI_DAILY_COST_HARD_STOP_USD:
        lines.append(
            f"🛑 Жёсткий стоп сработал (потолок ${config.AI_DAILY_COST_HARD_STOP_USD:.0f}) — "
            "тренер молчал до полуночи UTC"
        )
    elif config.AI_DAILY_COST_SOFT_CAP_USD > 0 and total_cost >= config.AI_DAILY_COST_SOFT_CAP_USD:
        lines.append(
            f"⚠️ Потолок расходов пройден (${config.AI_DAILY_COST_SOFT_CAP_USD:.0f}) — "
            "поиск, видео и разбор еды выключались"
        )
    return "\n".join(lines)


def _backup_dir() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH) or ".", "backups")


def _prune_stale_backups(backup_dir: str, keep: int) -> None:
    """Держит на диске только keep самых свежих (по имени — оно же дата)
    копий, удаляя остальное. keep<=0 значит «не чистить»."""
    if keep <= 0:
        return
    existing = sorted(f for f in os.listdir(backup_dir) if f.startswith(_BACKUP_PREFIX))
    for stale in existing[:-keep]:
        with suppress(OSError):
            os.remove(os.path.join(backup_dir, stale))


async def _rotate_disk_backup() -> str:
    """Вторая копия БД на диске рядом с рабочей — независимая от Telegram и от
    ADMIN_ID. Единственная копия раньше уходила одним документом в личку
    админа: удалённое сообщение, блокировка бота или смена ADMIN_ID при
    редеплое оставляли бота вообще без бэкапов, и никто бы не узнал об этом до
    аварии."""
    backup_dir = _backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    name = f"{_BACKUP_PREFIX}{dt.date.today().isoformat()}.db"
    path = os.path.join(backup_dir, name)
    if os.path.exists(path):
        # VACUUM INTO требует отсутствующий файл назначения — второй прогон в
        # те же сутки (например, после рестарта) иначе падает на ровном месте.
        os.remove(path)
    await db.backup_to_file(path)
    _prune_stale_backups(backup_dir, config.BACKUP_KEEP_COUNT)
    return path


def _latest_backup_age_hours() -> Optional[float]:
    """Часов с последнего успешного бэкапа на диске, или None, если их нет вовсе
    (свежий диск/первый запуск — это не тревога, а стартовое состояние)."""
    backup_dir = _backup_dir()
    if not os.path.isdir(backup_dir):
        return None
    files = [f for f in os.listdir(backup_dir) if f.startswith(_BACKUP_PREFIX)]
    if not files:
        return None
    newest = max(
        os.path.getmtime(os.path.join(backup_dir, f)) for f in files
    )
    return (dt.datetime.now().timestamp() - newest) / 3600


async def run_backup_staleness_check(bot: Bot) -> None:
    """Раз в час проверяет, не протухли ли бэкапы на диске, и чинит, если да.

    Отдельная задача, а не проверка внутри суточной джобы: суточная джоба сама
    может не запуститься (см. run_daily_admin_jobs) или упасть посреди работы
    — а именно это и надо заметить, а не только исправно отчитываться, когда
    всё и так хорошо.

    Чинит, а не только алертит: раньше эта проверка умела ровно одно — писать
    админу «проверь суточную джобу», и повторяла это каждый час, пока человек
    не дойдёт до контейнера руками. Копия при этом так и не появлялась. Причин
    у пропуска много (уехавшее расписание, умершая задача, неудачная запись на
    диск), а лечение одно — сделать копию сейчас, — поэтому оно и делается
    здесь, не дожидаясь следующего ADMIN_REPORT_HOUR.

    Сообщение админу уходит в любом случае, но разное: получилось — «отставал,
    сделал сам, суточная джоба всё равно сломана»; не получилось — «и починить
    не вышло» с текстом ошибки. Спама из этого не выходит: удачный догон
    обнуляет возраст, и следующий час проверку проходит молча.
    """
    while True:
        try:
            age = _latest_backup_age_hours()
            if age is not None and age > config.BACKUP_STALE_ALERT_HOURS:
                logger.error(
                    "DB backup is stale: last one is %.1f hours old (alert threshold %s)",
                    age, config.BACKUP_STALE_ALERT_HOURS,
                )
                await _repair_stale_backup(bot, age)
        except Exception:
            logger.exception("Backup staleness check failed")
        await asyncio.sleep(3600)


async def _repair_stale_backup(bot: Bot, age: float) -> None:
    """Догоняет пропущенную копию прямо из часовой проверки и рассказывает об
    этом админу. Ошибку записи не проглатывает: без неё «бэкапов нет» и «бэкап
    не пишется на диск» выглядят с той стороны одинаково."""
    error: Optional[str] = None
    try:
        await _rotate_disk_backup()
    except Exception as exc:
        logger.exception("Stale-backup repair failed")
        error = f"{type(exc).__name__}: {exc}"
    if not config.ADMIN_ID:
        return
    if error:
        text = (
            f"🛑 Бэкап базы не обновлялся {age:.0f} ч., и сделать копию сейчас не вышло: "
            f"{error}\nСмотри логи (admin_tasks._rotate_disk_backup)."
        )
    else:
        text = (
            f"⚠️ Бэкап базы отставал {age:.0f} ч. — копию сделал сам, база в порядке.\n"
            "Суточная джоба всё равно не отработала, проверь "
            "admin_tasks.run_daily_admin_jobs."
        )
    with suppress(Exception):
        await bot.send_message(chat_id=config.ADMIN_ID, text=text)


def _format_trained_users(rows) -> str:
    """Список тех, кто закрыл тренировку — под цифрой в суточном отчёте:
    сколько человек тренировалось само по себе не говорит, кто именно."""
    if not rows:
        return ""
    lines = [""]
    for row in rows:
        who = f"@{row['username']}" if row["username"] else str(row["telegram_id"])
        count = row["workouts"]
        lines.append(f"  └ {who}" + (f" ({count})" if count > 1 else ""))
    return "\n".join(lines)


async def _send_daily_report(bot: Bot, backup_path: Optional[str]) -> None:
    yesterday = dt.date.today() - dt.timedelta(days=1)
    yesterday_str = yesterday.isoformat()
    stats = await db.daily_workout_stats(yesterday_str)
    trained = await db.daily_trained_users(yesterday_str)
    cost_report = await _build_cost_report(yesterday_str)
    who_report = _format_trained_users(trained)
    await bot.send_message(
        chat_id=config.ADMIN_ID,
        text=(
            f"📊 Статистика за {yesterday.strftime('%d.%m.%Y')}\n"
            f"Потренировалось пользователей: {stats['users']}\n"
            f"Завершено тренировок: {stats['workouts']}"
            f"{who_report}"
            f"{cost_report}"
        ),
    )

    # Документом уходит та же копия, что уже легла на диск (см.
    # _rotate_disk_backup) — не делаем вторую только ради Telegram: если диск
    # уже подтвердил бэкап, доставка в личку админа лишь дублирует его, а не
    # является единственным способом его получить, как было раньше.
    if backup_path and os.path.exists(backup_path):
        await bot.send_document(
            chat_id=config.ADMIN_ID,
            document=FSInputFile(backup_path, filename=os.path.basename(backup_path)),
        )
    else:
        # Ротация на диске не удалась — тогда хотя бы личное сообщение
        # получает временную копию, чтобы день не остался вовсе без бэкапа.
        backup_name = f"{_BACKUP_PREFIX}{dt.date.today().isoformat()}.db"
        tmp_path = os.path.join(tempfile.gettempdir(), backup_name)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        try:
            await db.backup_to_file(tmp_path)
            await bot.send_document(chat_id=config.ADMIN_ID, document=FSInputFile(tmp_path, filename=backup_name))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


# ---------- утренний разбор поведения за вчера ------------------------------
#
# Сырой лог действий (/activity) отвечает на «что делал вот этот человек», но
# не на «как вчера пользовались ботом»: чтобы увидеть путь, надо прочитать
# сотни строк глазами. Разбор делает это раз в сутки и приходит одним
# сообщением вместе с утренним отчётом.

# Потолки — чтобы суточный лог гарантированно влезал в один запрос и не стоил
# как неделя работы тренера. Строк на человека хватает на полный сеанс, а
# людей — на всех активных за сутки при нынешних объёмах.
BEHAVIOUR_MAX_EVENTS = 4000
BEHAVIOUR_MAX_PER_USER = 80
BEHAVIOUR_LINE_LIMIT = 90

# Память разбора: сколько прошлых утр он видит и сколько от каждого читает.
# Три дня — чтобы «третий день подряд обрываются на Grab» было видно, а
# недельная простыня не съедала вход. Обрезка по символам — потому что память
# нужна выводами, а они у разбора всегда сверху.
BEHAVIOUR_MEMORY_DAYS = 3
BEHAVIOUR_MEMORY_CHARS = 1200

_BEHAVIOUR_MARKERS = {
    activity_log.KIND_CALLBACK: "👉",
    activity_log.KIND_CALLBACK_UNHANDLED: "💀",
    activity_log.KIND_CALLBACK_RECOVERED: "🔁",
    activity_log.KIND_AI_REPLY: "🤖",
    activity_log.KIND_REPLY_BUTTON: "👉",
    activity_log.KIND_AI_FAILED: "⚠️",
    activity_log.KIND_AI_PROGRAM_OFFERED: "🗂",
}


def _behaviour_day_bounds(day: dt.date) -> tuple[str, str]:
    """Границы суток админа (МСК) в том виде, в каком время лежит в базе (UTC).

    Сутки именно админские: он спрашивает «что было вчера», имея в виду своё
    вчера, а UTC-шные сутки отрезали бы вечер — самое живое время бота.
    """
    start = dt.datetime.combine(day, dt.time.min) - dt.timedelta(hours=config.ADMIN_TZ_OFFSET)
    return (
        start.isoformat(timespec="seconds"),
        (start + dt.timedelta(days=1)).isoformat(timespec="seconds"),
    )


def _behaviour_line(row) -> str:
    at = dt.datetime.fromisoformat(row["created_at"]) + dt.timedelta(hours=config.ADMIN_TZ_OFFSET)
    content = (row["content"] or "").replace("\n", " ⏎ ")
    if len(content) > BEHAVIOUR_LINE_LIMIT:
        content = content[: BEHAVIOUR_LINE_LIMIT - 1] + "…"
    marker = _BEHAVIOUR_MARKERS.get(row["kind"], "💬")
    return f"{at.strftime('%H:%M')} {marker} {content}"


async def build_behaviour_summary(day: dt.date) -> Optional[str]:
    """Материал для разбора: числа за сутки плюс лог, сгруппированный по людям.

    Группировка по людям, а не сплошная лента: разбирается путь одного человека,
    и вперемешку он не читается. Внутри человека — хронология, как он и шёл.

    None — суток без единого действия: разбирать нечего, и тратить на это вызов
    модели незачем.
    """
    since, until = _behaviour_day_bounds(day)
    total_events, total_people = await db.count_events_between(since, until)
    if not total_events:
        return None
    rows = await db.list_events_between(since, until, limit=BEHAVIOUR_MAX_EVENTS)

    by_user: dict[int, list] = {}
    names: dict[int, str] = {}
    for row in rows:
        by_user.setdefault(row["telegram_id"], []).append(row)
        names[row["telegram_id"]] = (
            f"@{row['username']}" if row["username"] else str(row["telegram_id"])
        )
    newcomers = await db.count_new_users_between(since, until)
    workouts, trained = await db.count_finished_workouts_between(since, until)

    head = [
        f"Сутки: {day.strftime('%d.%m.%Y')} (время московское).",
        f"Действий: {total_events}, людей: {total_people}, из них новых: {newcomers}.",
        f"Завершено тренировок: {workouts} у {trained} человек.",
    ]
    if total_events > len(rows):
        head.append(
            f"В лог ниже влезли первые {len(rows)} действий из {total_events} — "
            "остальное срезано, учитывай это в выводах."
        )

    parts = ["\n".join(head), ""]
    # Самые активные сверху: у них путь длиннее и разбирать там есть что.
    for telegram_id, events in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
        shown = events[:BEHAVIOUR_MAX_PER_USER]
        tail = "" if len(events) == len(shown) else f" (показаны первые {len(shown)} из {len(events)})"
        parts.append(f"— {names[telegram_id]}, действий {len(events)}{tail}:")
        parts.extend(_behaviour_line(row) for row in shown)
        parts.append("")
    return "\n".join(parts).strip()


async def build_behaviour_memory(day: dt.date) -> Optional[str]:
    """Чем разбор помнит прошлые утра — или None, если помнить пока нечего.

    Без памяти каждое утро разбирается с чистого листа: одни и те же гипотезы
    предлагаются заново («сделать один CTA после языка»), а сказать
    «обрывов на Grab стало меньше» не по чему — сравнивать не с чем. Здесь —
    прошлые разборы целиком (подрезанные), а не выжимка: выжимка из выжимки
    теряет ровно то конкретное место в логе, ради которого гипотеза и писалась.
    """
    rows = await db.list_behaviour_digests_before(day.isoformat(), BEHAVIOUR_MEMORY_DAYS)
    if not rows:
        return None
    parts = ["Твои разборы за предыдущие сутки (свежий первым):"]
    for row in rows:
        past = dt.date.fromisoformat(row["day"]).strftime("%d.%m.%Y")
        text = row["text"].strip()
        if len(text) > BEHAVIOUR_MEMORY_CHARS:
            text = text[: BEHAVIOUR_MEMORY_CHARS - 1] + "…"
        parts.append(f"=== {past} ===\n{text}")
    return "\n\n".join(parts)


async def _send_behaviour_digest(bot: Bot, day: dt.date) -> None:
    """Разбор поведения за сутки — отдельным сообщением после отчёта.

    Отдельным, а не абзацем в отчёте: у отчёта есть документ-бэкап, а тут текст
    на несколько абзацев, и склеенные они не читаются. Молчим, когда разбирать
    нечего или модель не ответила: пустое «данных нет» каждое утро — шум.
    """
    summary = await build_behaviour_summary(day)
    if summary is None:
        return
    memory = await build_behaviour_memory(day)
    text = await ai_trainer.behaviour_digest(summary, memory)
    if not text:
        return
    # Сохраняем до отправки: память следующего утра не должна зависеть от того,
    # дошло ли сообщение (админ мог заблокировать бота — разбор всё равно был).
    await db.save_behaviour_digest(day.isoformat(), text)
    header = f"🧠 <b>Как вчера пользовались — {day.strftime('%d.%m.%Y')}</b>\n\n"
    # Модель отвечает markdown'ом, и без разбора он доезжал звёздочками и
    # решётками прямо в текст (см. formatting.ai_markdown_to_html). Режем до
    # разбора, а не после: разрыв посреди <b> ломает и сообщение, и остаток.
    for index, chunk in enumerate(formatting.split_for_telegram(text, 3500)):
        await bot.send_message(
            chat_id=config.ADMIN_ID,
            text=(header if index == 0 else "") + formatting.ai_markdown_to_html(chunk),
            parse_mode="HTML",
        )


async def _run_retention_cleanup() -> None:
    """Стереть то, что дольше положенного лежит в базе — стоимость AI-вызовов,
    сырой лог действий, отметки о показанных предупреждениях лимита, отданные
    ссылки на общие тренировки."""
    await db.prune_old_cost_events(config.COST_EVENTS_RETENTION_DAYS)
    await db.prune_old_user_events(config.ACTIVITY_RETENTION_DAYS)
    await db.prune_old_behaviour_digests(config.BEHAVIOUR_DIGEST_RETENTION_DAYS)
    await db.prune_old_limit_acks()
    await db.prune_old_pushes(
        PUSH_RETENTION_DAYS,
        keep_categories=tuple(ann.key for ann in announcements.ANNOUNCEMENTS),
    )
    cutoff = (
        dt.datetime.now() - dt.timedelta(days=config.SHARED_ITEMS_RETENTION_DAYS)
    ).isoformat(timespec="seconds")
    await db.delete_shared_items_older_than(cutoff)


async def run_retention_cleanup_job() -> None:
    """Суточная чистка ретеншна — отдельной задачей, как и прополка OAuth.

    Раньше эти четыре prune-вызова жили внутри `_send_daily_report`, ПОСЛЕ
    `bot.send_message` админу: без `ADMIN_ID` весь `run_daily_admin_jobs`
    выходит сразу же (см. проверку в начале), а если админ заблокировал бота,
    `send_message` бросает исключение раньше, чем очередь доходит до чистки.
    В обоих случаях таблицы не чистились бы совсем — тот же самый долг, что
    был у прополки OAuth до её выделения (см. run_oauth_purge_job).
    """
    while True:
        await asyncio.sleep(_seconds_until_next_run(config.ADMIN_REPORT_HOUR))
        try:
            await _run_retention_cleanup()
        except Exception:
            logger.exception("Retention cleanup failed")


async def run_oauth_purge_job() -> None:
    """Прополка просрочки OAuth — отдельной задачей, раз в час.

    Отдельной, потому что раньше она стояла внутри суточного отчёта админу, за
    `bot.send_message`: админ заблокировал бота — исключение, и прополки в этот
    день нет вовсе. А без `ADMIN_ID` отчёт не запускается никогда, то есть
    таблицы не чистились бы совсем.

    Раз в час, а не в сутки: коды и заявки живут минуты, и держать их до ночи
    незачем — а именно они копятся от каждой брошенной попытки подключения.
    """
    while True:
        try:
            await db.purge_expired_oauth()
        except Exception:
            logger.exception("OAuth purge failed")
        await asyncio.sleep(3600)


async def _catch_up_missed_backup() -> None:
    """Бэкап сразу на старте, если суточное окно проехали.

    Расписание живёт только в памяти процесса: `_seconds_until_next_run`
    считается от «сейчас», и рестарт после ADMIN_REPORT_HOUR отправляет
    следующий запуск на сутки вперёд — пропущенный день не догонял никто. На
    проде это дало ровно 26.4 часа без копии: вечер деплоев, контейнер
    перезапускался, окно 07:00 проехали молча, и заметил это только часовой
    алерт (run_backup_staleness_check).

    Порог — сутки: при живом процессе возраст копии на любом рестарте лежит в
    0–24 часах, так что больше — это уже точно пропуск. Само по себе
    самоограничивается: первый же догон обнуляет возраст, и десять рестартов
    подряд не сделают десять копий.

    Бэкапов нет вовсе (свежий диск, первый запуск) — тоже делаем сразу, иначе
    новый инстанс живёт без единой копии до первого ADMIN_REPORT_HOUR.

    Отчёт админу отсюда НЕ шлём: он про «вчера» и привязан к своему часу, а
    рестарт может случиться когда угодно — дублировать его на каждом подъёме
    значило бы чинить бэкапы ценой спама.
    """
    try:
        age = _latest_backup_age_hours()
        if age is not None and age < config.BACKUP_CATCHUP_HOURS:
            return
        logger.warning(
            "Бэкап пропустил суточное окно (возраст %s) — делаю копию сейчас",
            "нет копий" if age is None else f"{age:.1f} ч",
        )
        await _rotate_disk_backup()
    except Exception:
        # Всё целиком, а не только запись копии: этот вызов стоит ПЕРЕД вечным
        # циклом суточной джобы, и любое исключение отсюда убивало бы задачу
        # молча — вместе с бэкапами на всю жизнь процесса.
        logger.exception("Catch-up DB backup failed")


async def run_daily_admin_jobs(bot: Bot) -> None:
    """Бэкап на диск идёт каждый день независимо от ADMIN_ID — раньше вся
    джоба (а с ней и единственный бэкап) не стартовала вовсе без своего
    аккаунта админа, и смена/потеря ADMIN_ID при редеплое молча останавливала
    бэкапы насовсем. Отчёт и документ в личку — по-прежнему только с ADMIN_ID.
    """
    await _catch_up_missed_backup()
    while True:
        await asyncio.sleep(_seconds_until_next_run(config.ADMIN_REPORT_HOUR))
        backup_path = None
        try:
            backup_path = await _rotate_disk_backup()
        except Exception:
            logger.exception("Daily DB backup failed")
        if not config.ADMIN_ID:
            continue
        try:
            await _send_daily_report(bot, backup_path)
        except Exception:
            logger.exception("Daily admin report failed")
        try:
            await _send_behaviour_digest(bot, dt.date.today() - dt.timedelta(days=1))
        except Exception:
            # Разбор — приятное дополнение к отчёту, а не сам отчёт: упавшая
            # модель (или её отсутствие) не должна выглядеть как сбой джобы.
            logger.exception("Daily behaviour digest failed")
