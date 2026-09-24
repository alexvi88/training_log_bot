"""Демо-аккаунт для App Review: вход по логину и паролю (POST /v1/auth/password)
и заполнение аккаунта правдоподобной историей.

Зачем: Beta App Review отклонил сборку по Guideline 2.1(a) — ревьюеру нужен
логин и пароль от аккаунта, в котором уже есть данные. Sign in with Apple
заводит пустой аккаунт, а код из Telegram ревьюеру взять негде. Обычным
пользователям вход по паролю не нужен и не предлагается: учётка одна, из
секретов окружения (config.REVIEW_DEMO_*), и без них ручки нет вовсе.

Аккаунт — обычный app-only (db.create_app_only_user, отрицательный
синтетический telegram_id), а стабильность между входами держит та же таблица,
что и у Apple, — auth_identities, провайдер `review_demo`, идентификатор —
логин из секрета. Если ревьюер удалит аккаунт (Apple это проверяет, 5.1.1(v)),
auth_identities уйдёт вместе с ним, и следующий вход заведёт и заполнит новый.

История пишется теми же функциями db.py, что и импорт CSV
(handlers/csv_import.apply_import): завершённая тренировка задним числом,
блоки, подходы, в конце achievement_sync.resync — поэтому рекорды, e1RM,
сводка и значки выводятся из подходов так же, как у живого атлета, а не
рисуются отдельно. Историю AI-тренера не заполняем нарочно.

Названия упражнений ниже — русские канонические имена шаблонов каталога
(seed_data.EXERCISE_TEMPLATES): это идентичность для поиска шаблона, а не
показываемый текст. Показываемое имя fork_exercise_from_template локализует
сам по users.lang.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import time
from typing import Optional

from starlette.requests import Request

import achievement_sync
import config
import db

PROVIDER = "review_demo"

# Перебор пароля: не больше FAILURE_LIMIT_PER_IP неудач за окно с одного
# адреса, дальше 429 до конца окна. В памяти процесса — как у
# mcp_oauth.RegisterRateLimitMiddleware: грубый предохранитель, переживать
# рестарт он не обязан, а пароль длинный и случайный.
FAILURE_WINDOW_SECONDS = 600
FAILURE_LIMIT_PER_IP = 10
_failures: dict[str, list[float]] = {}
# Выше этого числа адресов в словаре — чистим протухшие ключи, чтобы поток
# неудач с разных адресов не раздувал память процесса навсегда.
_FAILURES_PRUNE_AT = 1024

# Первый вход и заполнение — одна критическая секция: два параллельных входа
# иначе оба не нашли бы аккаунт (или оба увидели ноль тренировок) и завели бы
# два аккаунта или удвоили историю. Замок создаётся лениво под текущий цикл —
# asyncio.Lock привязывается к циклу первого конкурентного захвата, а у тестов
# цикл на каждый тест свой (та же история, что с db._write_lock в conftest).
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


# ---------- вход ----------

def client_ip(request: Request) -> str:
    """Адрес для счётчика неудач. На Fly настоящий адрес клиента приносит
    Fly-Client-IP (его ставит прокси Fly), иначе — последний элемент
    X-Forwarded-For (его дописывает прокси, см. mcp_oauth._client_ip), иначе —
    адрес соединения."""
    fly_ip = request.headers.get("fly-client-ip", "").strip()
    if fly_ip:
        return fly_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "(unknown)"


def _recent(ip: str, now: float) -> list[float]:
    start = now - FAILURE_WINDOW_SECONDS
    return [t for t in _failures.get(ip, []) if t > start]


def is_rate_limited(ip: str) -> bool:
    return len(_recent(ip, time.monotonic())) >= FAILURE_LIMIT_PER_IP


def record_failure(ip: str) -> None:
    now = time.monotonic()
    hits = _recent(ip, now)
    hits.append(now)
    _failures[ip] = hits
    if len(_failures) > _FAILURES_PRUNE_AT:
        for key in list(_failures):
            if not _recent(key, now):
                del _failures[key]


def reset_failures() -> None:
    """Для тестов: счётчик живёт в памяти процесса, а тесты — в одном процессе."""
    _failures.clear()


def check_credentials(username: str, password: str) -> bool:
    """Сравнение за постоянное время, и логин, и пароль сравниваются всегда —
    без короткого замыкания, чтобы по времени ответа нельзя было угадать
    даже правильный логин."""
    user_ok = hmac.compare_digest(
        username.encode("utf-8"), config.REVIEW_DEMO_USERNAME.encode("utf-8")
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"), config.REVIEW_DEMO_PASSWORD.encode("utf-8")
    )
    return user_ok & pass_ok


async def ensure_demo_user(lang: Optional[str] = None) -> int:
    """user_id демо-аккаунта: найти по auth_identities или завести, и
    заполнить историей, если тренировок у него ещё нет. Язык — только для
    НОВОГО аккаунта (дефолт английский: ревьюеры Apple читают по-английски);
    существующему язык не переписываем — его могли переключить в настройках."""
    username = config.REVIEW_DEMO_USERNAME
    async with _get_lock():
        user_id = await db.resolve_auth_identity(PROVIDER, username)
        if user_id is not None and await db.get_user(user_id) is None:
            user_id = None
        if user_id is None:
            user = await db.create_app_only_user(language_code=lang or "en")
            user_id = user["telegram_id"]
            await db.link_auth_identity(user_id, PROVIDER, username)
        if await db.count_workouts(user_id) == 0 and await db.count_workouts(user_id, "active") == 0:
            await seed_history(user_id)
    return user_id


# ---------- заполнение ----------

SQUAT = "Присед со штангой"
BENCH = "Жим штанги лёжа"
ROW = "Тяга штанги в наклоне"
DEADLIFT = "Становая тяга"
OHP = "Жим штанги стоя"
PULLUP = "Подтягивания"

TOTAL_WORKOUTS = 12
TRAINING_WEEKDAYS = (0, 2, 4)  # пн, ср, пт
SESSION_START_HOUR = 18  # местное время атлета

# Шаг времени внутри тренировки — чтобы длительность на карточке (разбег меток
# подходов) выходила похожей на живую: минут 50-60.
_FIRST_SET_AFTER = dt.timedelta(minutes=6)
_BETWEEN_SETS = dt.timedelta(minutes=2, seconds=30)
_BETWEEN_EXERCISES = dt.timedelta(minutes=4)
_FINISH_AFTER_LAST_SET = dt.timedelta(minutes=3)


def _sets_for(day: str, k: int) -> list[tuple[str, list[tuple[float, int]]]]:
    """Состав тренировки: A — присед, жим лёжа, тяга в наклоне; B — становая,
    жим стоя, подтягивания. k — какая это по счёту тренировка своего типа
    (0…5): линейная прогрессия новичка, так что e1RM растёт от сессии к
    сессии и рекорды появляются сами собой."""
    if day == "A":
        squat = 80 + 2.5 * k
        bench = 60 + 2.5 * k
        row = 50 + 2.5 * k
        bench_reps = [5, 5, 5, 5, 4] if k == 5 else [5] * 5
        return [
            (SQUAT, [(60.0, 5)] + [(squat, 5)] * 5),
            (BENCH, [(40.0, 8)] + [(bench, r) for r in bench_reps]),
            (ROW, [(row, 8)] * 3),
        ]
    deadlift = 100 + 5 * k
    ohp = 40 + 2.5 * (k // 2)
    ohp_reps = 5 + (k % 2)
    pull = 7 + k // 2
    return [
        (DEADLIFT, [(70.0, 5), (deadlift, 5)]),
        (OHP, [(30.0, 8)] + [(ohp, ohp_reps)] * 4),
        (PULLUP, [(0.0, pull + 1), (0.0, pull), (0.0, pull)]),
    ]


def _session_days(today: dt.date) -> list[dt.date]:
    """Последние TOTAL_WORKOUTS тренировочных дней (пн/ср/пт) строго до
    сегодняшнего — старые первыми. Вчерашняя или позавчерашняя тренировка
    делает сводку «живой», а не заброшенной месяц назад."""
    days: list[dt.date] = []
    day = today - dt.timedelta(days=1)
    while len(days) < TOTAL_WORKOUTS:
        if day.weekday() in TRAINING_WEEKDAYS:
            days.append(day)
        day -= dt.timedelta(days=1)
    return list(reversed(days))


async def _exercise_ids(user_id: int) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name in (SQUAT, BENCH, ROW, DEADLIFT, OHP, PULLUP):
        template = await db._find_global_template_by_name(name)
        if template is None:
            raise RuntimeError(f"catalog template not found: {name!r}")
        ids[name] = await db.fork_exercise_from_template(user_id, template["id"])
    return ids


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


async def seed_history(user_id: int, today: Optional[dt.date] = None) -> int:
    """Записать демо-историю. Возвращает число записанных тренировок.

    Время хранится как у всей базы — по часам сервера (UTC, db.now_iso), а
    местный день атлета восстанавливается прибавлением tz_offset; поэтому
    «18:00 у атлета» — это 18:00 минус его пояс, тот же приём, что у
    handlers/csv_import.apply_import.
    """
    tz_offset = await db.user_tz_offset(user_id)
    days = _session_days(today or dt.datetime.now().date())
    ids = await _exercise_ids(user_id)

    def local(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hour, minute)) - dt.timedelta(hours=tz_offset)

    # Вес тела — раз в неделю утром, начиная за день до первой тренировки:
    # подтягивания считают нагрузку от веса тела на дату тренировки
    # (db.bodyweight_at), и без взвешивания раньше первой сессии у первых
    # подтягиваний нагрузки не было бы вовсе.
    weights = (82.4, 82.0, 81.9, 81.5, 81.2)
    first = days[0] - dt.timedelta(days=1)
    for i, weight in enumerate(weights):
        day = first + dt.timedelta(days=7 * i)
        if day >= (today or dt.datetime.now().date()):
            break
        await db.add_bodyweight_log(user_id, weight, _iso(local(day, 8, 15)))

    counters = {"A": 0, "B": 0}
    written = 0
    for i, day in enumerate(days):
        kind = "A" if i % 2 == 0 else "B"
        plan = _sets_for(kind, counters[kind])
        counters[kind] += 1

        started = local(day, SESSION_START_HOUR)
        moment = started + _FIRST_SET_AFTER
        # finished_at уточняется ниже, когда станет известна метка последнего
        # подхода; пока — заведомо после всех подходов.
        workout_id = await db.create_finished_workout(
            user_id, _iso(started), _iso(started + dt.timedelta(hours=2))
        )
        stamps: list[tuple[int, str]] = []
        for ex_index, (name, sets) in enumerate(plan):
            if ex_index:
                moment += _BETWEEN_EXERCISES
            ex_id = ids[name]
            block_id = await db.create_block(workout_id, "single")
            await db.add_block_exercise(block_id, ex_id, 0)
            for idx, (weight, reps) in enumerate(sets, start=1):
                set_id = await db.add_set(block_id, ex_id, idx, 0, weight, reps)
                stamps.append((set_id, _iso(moment)))
                moment += _BETWEEN_SETS
        await db.set_set_timestamps(stamps)
        finished = moment - _BETWEEN_SETS + _FINISH_AFTER_LAST_SET
        await db.update_workout_date(workout_id, _iso(started), _iso(finished))
        written += 1

    for ex_id in ids.values():
        await db.touch_exercise_last_used(ex_id)
    await achievement_sync.resync(user_id)
    return written
