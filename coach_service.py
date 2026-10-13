"""Сервис для тренеров: живой тренер смотрит дневник своих атлетов.

Интерфейс — только iOS-приложение (REST — api_v1_coach.py), бота у фичи нет.
Здесь правила и расчёты, HTTP и коды ошибок — в api_v1_coach.

Связка и согласие. Тренер нажимает «Я тренер» (coach_profiles: имя, под которым
его видят атлеты) и выдаёт код приглашения (coach_invites). Код сам по себе
ничего не открывает: атлет видит, кто тренер и что именно тот увидит, и только
его «Разрешить» заводит строку coach_links. Отозвать может любая сторона —
строка удаляется, доступ кончается в тот же момент.

Что тренер видит (VISIBLE_ALWAYS) и чего не видит никогда — одно место правды,
его же показывает экран согласия у атлета:

- всегда: тренировки, подходы, заметки к ним, рекорды, программы и прогресс по
  ним;
- только если атлет включил: вес тела (show_bodyweight), по умолчанию
  выключен;
- никогда: переписка с AI-тренером (и его разборы тренировок), почта, Apple ID,
  ник и id в Telegram. Тренер знает атлета по имени, которым тот сам назвался
  на экране согласия.

AI-сводка для тренера отдаёт модели данные атлетов по тому же правилу, что
остальные фоновые вызовы модели (`ai_trainer._background_ai_allowed`): атлет
приложения — только с согласием (users.ai_consent_at), чисто телеграмный — как
недельный итог и комментарий после тренировки, без отдельного листа согласия.
Остальные в сводку не попадают, и ответ говорит, сколько их.

Строк, вычисленных на импорте, в модуле нет — язык берётся в момент вызова
(см. CLAUDE.md, «Ловушка, встретившаяся шесть раз»).
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any, Optional

import acquisition
import ai_limits
import ai_trainer
import analytics
import config
import db
import formatting
import i18n
import progress_data
import timeutil
from handlers import sharing

MAX_DISPLAY_NAME_LENGTH = 40
MAX_COMMENT_LENGTH = 1000

# Код приглашения: читается вслух и вводится руками — без 0/O, 1/I/L.
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_LENGTH = 8

# Схема приложения (RootView.onOpenURL у iOS). В тексте приглашения её больше
# нет: мессенджеры не делают её кликабельной, а без приложения она не делает
# ничего. Живёт кнопкой «Открыть в приложении» на странице приглашения и
# запасным `app_link`, пока у сервера не задан публичный адрес.
APP_LINK_PREFIX = "trainingdiary://coach/"

# Страница приглашения на сервере: `<config.public_base_url()>/coach/<код>`
# (coach_invite_page.py). Открывается у всех — и в браузере без приложения, и
# Universal Link'ом в приложении (apple-app-site-association).
WEB_LINK_PATH = "/coach/"

# Окна сигналов списка клиентов.
MISSING_DAYS = 7
RECORD_WINDOW_DAYS = 7
PROGRAM_BEHIND_DAYS = 7

# «Прогресс клиента» (client_progress): окно недель по умолчанию и его
# границы у `?weeks=`, сколько основных движений на графике, сколько
# упражнений в таблице рекордов и окно «свежих» рекордов.
PROGRESS_WEEKS = 12
PROGRESS_MIN_WEEKS = 4
PROGRESS_MAX_WEEKS = 52
PROGRESS_LIFTS = 3
PROGRESS_BEST_EXERCISES = 8
PROGRESS_RECORD_DAYS = 28

# Что видно тренеру — для справки и для тестов; экран согласия в приложении
# говорит это своим текстом.
VISIBLE_ALWAYS = ("workouts", "sets", "notes", "records", "programs")
VISIBLE_OPT_IN = ("bodyweight",)
NEVER_VISIBLE = ("ai_chats", "email", "apple_id", "telegram")


def new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


def normalize_invite_code(raw: str) -> str:
    """Код из того, что вставил человек: сам код, «abcd-2345», ссылка
    https://<сервер>/coach/ABCD2345 или старая trainingdiary://coach/ABCD2345.
    Регистр, пробелы и дефисы не важны; хвост `?…`/`#…` у ссылки — тоже."""
    text = (raw or "").strip()
    if "/" in text:
        text = text.split("#", 1)[0].split("?", 1)[0]
        text = text.rstrip("/").rsplit("/", 1)[-1]
    return "".join(ch for ch in text.upper() if ch.isalnum())


def clean_display_name(raw: Any) -> Optional[str]:
    """Имя без управляющих символов и лишних пробелов; None — пустое."""
    if not isinstance(raw, str):
        return None
    name = " ".join(formatting.strip_control_chars(raw).split())
    return name or None


def web_link(code: str) -> Optional[str]:
    """https-ссылка на страницу приглашения или None без публичного адреса."""
    base = config.public_base_url()
    return f"{base}{WEB_LINK_PATH}{code}" if base else None


def invite_link(code: str) -> str:
    """Главная ссылка приглашения — та, что уходит в текст «Поделиться»."""
    return web_link(code) or f"{APP_LINK_PREFIX}{code}"


def telegram_link(bot_username: str, code: str) -> str:
    """Приглашение прямо в бота: `/start coach_<код>` (handlers/coach.py)."""
    return f"https://t.me/{bot_username}?start={acquisition.COACH_PREFIX}{code}"


def invite_json(code: str) -> dict[str, Any]:
    # `app_link` — по историческому имени поля: приложение вставляет его в
    # текст «Поделиться» как есть, поэтому здесь https-ссылка на страницу
    # приглашения, а не схема trainingdiary://, которую мессенджеры не
    # открывают. Старые сборки получают рабочую ссылку без обновления.
    return {"code": code, "app_link": invite_link(code)}


def shown_exercise_names(names: list[str]) -> list[str]:
    """Имена упражнений атлета глазами тренера: каталожное — на языке
    тренера (текущий язык запроса/апдейта), своё имя атлета — как есть.
    У атлета упражнение лежит под именем на ЕГО языке, и без этого
    англоязычный тренер читал «Жим штанги лёжа» в карточке русского атлета."""
    return [sharing.shown_exercise_name(name) for name in names]


def visibility_json(link: Any) -> dict[str, Any]:
    """Только то, что атлет переключает сам. Всегда видное (VISIBLE_ALWAYS) и
    никогда не видное (NEVER_VISIBLE) — правило продукта, а не настройка:
    экран согласия в приложении пишет его своим текстом."""
    return {
        "bodyweight": bool(link["show_bodyweight"]) if link is not None else False,
    }


# ---------- сигналы по атлету ----------


async def client_signals(client: Any, link: Any, coach_id: int) -> dict[str, Any]:
    """Сигналы строки «Мои клиенты»: тренировался ли на этой неделе, сколько
    дней без зала, свежие рекорды, отстаёт ли от назначенной программы.

    Только факты из дневника: нет данных — нет сигнала (TONE_OF_VOICE, «Пуш
    обязан быть правдой»)."""
    client_id = client["telegram_id"]
    today = timeutil.user_today(client)
    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(client_id)]
    week_start = today - dt.timedelta(days=today.weekday())
    this_week = sum(1 for d in dates if week_start <= d <= today)
    last = max(dates) if dates else None
    days_since = (today - last).days if last is not None else None
    linked_on = dt.date.fromisoformat(link["created_at"][:10])
    # Без единой тренировки «пропадает» тот, кто открыл дневник неделю назад
    # и так и не пришёл в зал.
    quiet_days = days_since if days_since is not None else (today - linked_on).days
    missing = quiet_days >= MISSING_DAYS
    records = await recent_records(client)
    behind = False
    for assignment in await db.list_coach_assignments(coach_id, client_id):
        if not await _program_on_track(client_id, assignment):
            behind = True
            break
    return {
        "workouts_this_week": this_week,
        "last_workout_at": last.isoformat() if last is not None else None,
        "days_since_last_workout": days_since,
        "missing": missing,
        "new_records": len(records),
        "behind_program": behind,
    }


async def _program_on_track(client_id: int, assignment: Any) -> bool:
    """Назначена неделю назад и больше — и за последнюю неделю по ней ни одной
    тренировки: отстаёт. Свежее назначение ещё не повод."""
    now = db.utc_now_naive()
    assigned_at = dt.datetime.fromisoformat(assignment["created_at"])
    if now - assigned_at < dt.timedelta(days=PROGRAM_BEHIND_DAYS):
        return True
    since = (now - dt.timedelta(days=PROGRAM_BEHIND_DAYS)).isoformat(timespec="seconds")
    return await db.count_program_workouts_since(client_id, assignment["program_id"], since) > 0


async def recent_records(client: Any, days: Optional[int] = None) -> list[dict[str, Any]]:
    """Рекорды e1RM за последние `days` дней (по умолчанию RECORD_WINDOW_DAYS)
    — тот же критерий, что у «Итога недели» (db.e1rm_records_in_window)."""
    days = days or RECORD_WINDOW_DAYS
    today = timeutil.user_today(client)
    start = today - dt.timedelta(days=days - 1)
    unit = formatting.unit_label(client["unit"])
    out = []
    for row in await db.e1rm_records_in_window(
        client["telegram_id"], start.isoformat(), today.isoformat(), client["e1rm_formula"]
    ):
        out.append({
            "exercise_id": row["exercise_id"],
            "exercise": sharing.shown_exercise_name(row["display_name"]),
            "weight": row["weight"],
            "reps": row["reps"],
            "e1rm": round(row["e1rm"], 1),
            # «62.5×9» — вид подхода у графиков и рекордов прогресса.
            "best_set": formatting.format_set(row["weight"], row["reps"]),
            # Тот же вид, что у рекордов «Итога недели» и у подходов в
            # приложении: «42.5кг × 8» (`weekly.record.best`).
            "text": i18n.t(
                "weekly.record.best", weight=f"{formatting.format_weight(row['weight'])}{unit}", reps=row["reps"]
            ),
        })
    return out


async def assigned_programs(coach_id: int, client_id: int) -> list[dict[str, Any]]:
    """Назначенные тренером программы атлета и какие дни сделаны после
    назначения — «тренер видит, какие дни выполнены»."""
    out = []
    for assignment in await db.list_coach_assignments(coach_id, client_id):
        last_by_day = await db.last_finished_by_routine(client_id, assignment["program_id"])
        days = []
        for day in await db.list_program_days_by_id(assignment["program_id"]):
            last_at = last_by_day.get(day["id"])
            done_at = last_at if last_at is not None and last_at >= assignment["created_at"] else None
            days.append({
                "id": day["id"],
                "name": day["name"],
                "exercise_count": day["exercise_count"],
                "done_at": done_at,
            })
        out.append({
            "program_id": assignment["program_id"],
            "name": assignment["program_name"],
            "assigned_at": assignment["created_at"],
            "days": days,
            "done_days": sum(1 for d in days if d["done_at"] is not None),
            "on_track": await _program_on_track(client_id, assignment),
        })
    return out


# ---------- прогресс клиента ----------


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _local_week(started_at: str, client: Any) -> dt.date:
    local = timeutil.to_user_local(dt.datetime.fromisoformat(started_at), client)
    return _week_start(local.date())


def _lift_points(
    series: progress_data.ChartSeries, client: Any, first_week: dt.date
) -> list[dict[str, Any]]:
    """Точка на НЕДЕЛЮ: лучшая тренировка недели по той же величине, что у
    графика упражнения (progress_data.chart_series — e1RM или повторы у
    упражнения своим весом). Недели без этого упражнения — без точки, а не
    нулём: ноль на графике силы читался бы как обвал."""
    best: dict[dt.date, tuple[float, Any]] = {}
    for session, (_when, value) in zip(series.sessions, series.points, strict=True):
        week = _local_week(session.started_at, client)
        if week < first_week:
            continue
        if week not in best or value > best[week][0]:
            best[week] = (value, session)
    points = []
    for week in sorted(best):
        value, session = best[week]
        top = session.top_set
        points.append({
            "week_start": week.isoformat(),
            "value": int(value) if series.is_bodyweight else round(value, 1),
            "top_set": formatting.format_set(top.weight, top.reps) if top else None,
        })
    return points


async def client_progress(client: Any, weeks: int = PROGRESS_WEEKS) -> dict[str, Any]:
    """«Прогресс клиента» глазами тренера: частота и тоннаж по неделям,
    лучшая тренировка недели в основных движениях, рекорды за всё время и
    свежие рекорды. Один расчёт на бота (handlers/coach.py) и REST
    (`GET /coach/clients/{id}/progress`).

    Только числа и имена: каталожное упражнение — на языке тренера
    (`shown_exercise_names`), своё имя атлета — как есть. Веса — в единице
    атлета (`unit` рядом). Недели — местные для атлета, с понедельника, от
    старых к новым, последняя — текущая (неполная). Данных мало — списки
    пустые, а не заполненные догадкой: что из этого рисовать, решает экран.
    """
    client_id = client["telegram_id"]
    formula = client["e1rm_formula"]
    tz = timeutil.offset_hours(client)
    today = timeutil.user_today(client)
    this_week = _week_start(today)
    first_week = this_week - dt.timedelta(weeks=weeks - 1)
    week_starts = [first_week + dt.timedelta(weeks=i) for i in range(weeks)]

    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(client_id, tz_offset=tz)]
    counts = {w: 0 for w in week_starts}
    for day in dates:
        week = _week_start(day)
        if week in counts:
            counts[week] += 1
    tonnage = {w: 0.0 for w in week_starts}
    for day, total in (
        await db.daily_tonnage(client_id, first_week.isoformat(), today.isoformat(), tz_offset=tz)
    ).items():
        week = _week_start(dt.date.fromisoformat(day))
        if week in tonnage:
            tonnage[week] += total or 0.0
    in_window = sum(counts.values())

    sessions_cache: dict[int, list[Any]] = {}

    async def sessions_of(exercise_id: int) -> list[Any]:
        if exercise_id not in sessions_cache:
            sessions_cache[exercise_id] = await progress_data.load_sessions(exercise_id, formula)
        return sessions_cache[exercise_id]

    lifts = []
    for row in await db.top_exercises_by_frequency(
        client_id, first_week.isoformat(), today.isoformat(), limit=PROGRESS_LIFTS, tz_offset=tz,
    ):
        series = progress_data.chart_series(await sessions_of(row["id"]))
        points = _lift_points(series, client, first_week)
        if not points:
            continue
        lifts.append({
            "exercise_id": row["id"],
            "exercise": sharing.shown_exercise_name(row["display_name"]),
            "metric": "reps" if series.is_bodyweight else "e1rm",
            "metric_label": i18n.t("history.chart_metric_reps") if series.is_bodyweight else "e1RM",
            "points": points,
        })

    records = []
    if dates:
        for row in await db.top_exercises_by_frequency(
            client_id, min(dates).isoformat(), today.isoformat(),
            limit=PROGRESS_BEST_EXERCISES, min_sessions=1, tz_offset=tz,
        ):
            sessions = await sessions_of(row["id"])
            is_bw = progress_data.chart_series(sessions).is_bodyweight
            book = analytics.gold_book(sessions, formula)
            summary = progress_data.best_set_json(book, is_bw)
            if book is None or summary is None:
                continue
            records.append({
                "exercise_id": row["id"],
                "exercise": sharing.shown_exercise_name(row["display_name"]),
                "metric": "reps" if is_bw else "e1rm",
                "sessions": row["sessions"],
                # Больше всего повторов в одном подходе — рекорд упражнения
                # своим весом, где вес снаряда ноль.
                "best_reps": book.max_reps,
                **summary,
            })

    return {
        "unit": client["unit"],
        "weeks": weeks,
        "total_workouts": len(dates),
        "frequency": {
            "workouts": in_window,
            "per_week": round(in_window / weeks, 1),
            "this_week": counts[this_week],
        },
        "weekly": [
            {"week_start": w.isoformat(), "workouts": counts[w], "tonnage": round(tonnage[w], 1)}
            for w in week_starts
        ],
        "lifts": lifts,
        "records": records,
        "recent_records": await recent_records(client, PROGRESS_RECORD_DAYS),
        "recent_record_days": PROGRESS_RECORD_DAYS,
    }


# ---------- назначение программы ----------


class AssignError(Exception):
    """Назначить нечего или некуда: code — машинный код для api_v1_coach."""

    def __init__(self, code: str, human: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.human = human


async def assign_program(coach_id: int, client_id: int, source_program_id: int) -> tuple[int, str]:
    """Копия программы тренера у атлета: свои дни, упражнения атлета (тот же
    резолв, что у шаринга — своё по имени, форк шаблона каталога, иначе новое),
    схемы и правила прогрессии. Возвращает (program_id копии, её имя).

    Копия, а не ссылка: дальше программа живёт у атлета как своя, а правка у
    тренера не переписывает тренировки атлета задним числом."""
    program = await db.get_program(source_program_id)
    if program is None or program["user_id"] != coach_id:
        raise AssignError("not_found")
    days = []
    for day in await db.list_program_days_by_id(source_program_id):
        exercises = await db.list_routine_exercises(day["id"])
        if exercises:
            days.append((day, exercises))
    if not days:
        raise AssignError("coach_program_empty")
    if await db.routine_budget(client_id, len(days)):
        raise AssignError("client_routine_limit")

    name = await sharing._dedupe_program_name(client_id, program["name"], None)
    new_id = await db.create_program(
        client_id, name, source="coach", description=program["description"],
        deload_every_weeks=program["deload_every_weeks"],
    )
    if new_id is None:  # имя заняли между проверкой и записью
        raise AssignError("name_taken")
    for day, exercises in days:
        routine_id = await db.create_routine(client_id, day["name"], program_id=new_id)
        progressions: dict[int, Any] = {}
        order = 0
        seen: set[int] = set()
        for ex in exercises:
            source_ex = await db.get_exercise(ex["exercise_id"])
            # Идентичность каталога, а не показанное имя: тренер с английским
            # интерфейсом и атлет с русским должны получить одно упражнение.
            identity = (source_ex["original_name"] if source_ex is not None else None) or ex["display_name"]
            ex_id = await sharing._resolve_exercise(client_id, identity)
            if ex_id in seen:
                continue
            seen.add(ex_id)
            await db.add_routine_exercise(routine_id, ex_id, order, ex["target"])
            if ex["progression"]:
                progressions[ex_id] = ex["progression"]
            order += 1
        if progressions:
            for row in await db.list_routine_exercises(routine_id):
                if row["exercise_id"] in progressions:
                    await db.set_routine_exercise_progression(row["id"], progressions[row["exercise_id"]])
    await db.add_coach_assignment(coach_id, client_id, new_id)
    return new_id, name


# ---------- AI-сводка для тренера ----------


async def _client_brief(coach_id: int, client: Any, link: Any) -> str:
    """Атлет текстом для модели: имя, неделя, пропуск, рекорды, назначенные
    программы, последние тренировки. Только то, что тренер и так видит на
    экранах — ни веса тела, ни переписки с AI-тренером."""
    signals = await client_signals(client, link, coach_id)
    lines = [i18n.t("coach.ai.data.client", name=link["client_name"])]
    lines.append(i18n.t("coach.ai.data.week", n=signals["workouts_this_week"]))
    if signals["days_since_last_workout"] is None:
        lines.append(i18n.t("coach.ai.data.no_workouts"))
    else:
        lines.append(i18n.t("coach.ai.data.days_since", n=signals["days_since_last_workout"]))
    for record in await recent_records(client):
        lines.append(i18n.t("coach.ai.data.record", exercise=record["exercise"], best=record["text"]))
    for program in await assigned_programs(coach_id, client["telegram_id"]):
        lines.append(i18n.t(
            "coach.ai.data.program", name=program["name"], done=program["done_days"],
            total=len(program["days"]),
        ))
    workouts = await db.list_workouts(client["telegram_id"], limit=6)
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    for w in workouts:
        names, set_count = contents.get(w["id"], ([], 0))
        lines.append(i18n.t(
            "coach.ai.data.workout", date=w["started_at"][:10],
            exercises=", ".join(shown_exercise_names(names[:6])),
            n=set_count,
        ))
    return "\n".join(lines)


async def _model_answer(coach_id: int, messages: list[dict[str, Any]]) -> str:
    """Один нестримящий платный вызов — через ai_trainer.paid_call (лимит
    вопроса тренера → вызов → цена в лог). LimitBlocked летит наружу."""
    client = ai_trainer._get_client()
    response = await ai_trainer.paid_call(
        coach_id, ai_limits.KIND_QUESTION,
        lambda: client.chat.completions.create(
            model=config.GROK_MODEL,
            max_tokens=900,
            extra_body={"reasoning_effort": config.GROK_QUICK_REASONING_EFFORT},
            extra_headers=ai_trainer._cache_headers(coach_id, scope="coach_summary"),
            messages=messages,
        ),
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()


async def ai_summary(coach_id: int, client_id: Optional[int] = None) -> dict[str, Any]:
    """Сводка по всем клиентам («кого пора пнуть», рекорды, застои) или по
    одному. Язык — тренера (выставлен запросом). Клиенты, чьи данные модели
    отдавать нельзя (`ai_trainer._background_ai_allowed`), в данные не
    попадают — их число уходит в `skipped_no_consent`."""
    links = await db.list_coach_clients(coach_id)
    if client_id is not None:
        links = [link for link in links if link["client_id"] == client_id]
    briefs = []
    skipped = 0
    for link in links:
        client = await db.get_user(link["client_id"])
        if client is None:
            continue
        if not await ai_trainer._background_ai_allowed(client):
            skipped += 1
            continue
        briefs.append(await _client_brief(coach_id, client, link))
    if not briefs:
        return {"text": None, "clients": 0, "skipped_no_consent": skipped}
    coach = await db.get_user(coach_id)
    today = (timeutil.user_today(coach) if coach is not None else dt.date.today()).isoformat()
    prompt_key = "coach.ai.system_one" if client_id is not None else "coach.ai.system_all"
    messages = [
        {"role": "system", "content": ai_trainer._with_language_tail(i18n.t(prompt_key))},
        {"role": "user", "content": i18n.t("coach.ai.data.today", date=today) + "\n\n" + "\n\n".join(briefs)},
    ]
    text = await _model_answer(coach_id, messages)
    await db.try_increment_ai_question_count(coach_id, config.AI_QUESTION_DAILY_LIMIT)
    return {
        "text": text or i18n.t("coach.ai.empty"),
        "clients": len(briefs),
        "skipped_no_consent": skipped,
    }

