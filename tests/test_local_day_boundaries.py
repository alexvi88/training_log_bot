"""Границы суток по часам пользователя, а не по UTC.

Сервер пишет started_at/created_at в UTC, а экраны считают «сегодня» через
timeutil.user_today, то есть по местному времени пользователя. Пока агрегаты в
базе резали день по UTC (`date(started_at)`), эти две системы расходились ровно
на тренировках около полуночи — и расхождение вылезало наружу двумя способами:

* «0 тренировок за 30 дней» сразу после закрытой тренировки — её UTC-день
  оказывался «завтра» относительно местного «сегодня», и окно её не ловило;
* «дней с последней тренировки: -1» — та же дата «в будущем», уже в счётчике.

Тесты ниже строят обе ситуации на фиксированных датах (никакой зависимости от
реальных часов): пользователь с ненулевым tz_offset и тренировка в 22-23 часа
местного времени.
"""
import datetime as dt

import analytics
import timeutil

BENCH = "Жим штанги лёжа"
SQUAT = "Присед со штангой"


async def _own(db, user_id, template_name):
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == template_name
    )
    return await db.fork_exercise_from_template(user_id, template["id"])


async def _session(db, user_id, stamp_utc: str, sets=()):
    """Завершённая тренировка с UTC-меткой `stamp_utc`; `sets` — [(ex_id, вес, повторы, сколько)]."""
    workout_id = await db.create_finished_workout(user_id, stamp_utc, stamp_utc)
    for ex_id, weight, reps, count in sets:
        block_id = await db.create_block(workout_id, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        for i in range(count):
            await db.add_set(block_id, ex_id, i, 0, weight, reps)
    return workout_id


# ---------- симптом 1: тренировка около полуночи выпадала из окна ----------


async def test_workout_dates_are_local_days_east_of_utc(fresh_db, user_id):
    """UTC+3, 22:00 UTC — это уже следующий день по местному календарю."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=3)
    await _session(db, user_id, "2026-07-23T22:00:00")

    assert await db.list_finished_workout_dates(user_id) == ["2026-07-24"]


async def test_dashboard_counts_tonight_workout_east_of_utc(fresh_db, user_id):
    """«0 тренировок за 30 дней» сразу после закрытой тренировки: у UTC+3
    закрытая в 01:00 местного времени тренировка лежит в UTC на прошлых сутках,
    и окно, которое считается от местного «сегодня», её не видело."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=3)
    user = await db.get_user(user_id)
    await _session(db, user_id, "2026-07-23T22:00:00")  # 24 июля, 01:00 по местному
    today = timeutil.to_user_local(dt.datetime(2026, 7, 23, 22, 30), user).date()

    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    dash = analytics.compute_dashboard(dates, today)

    assert dash.last_30_days == 1
    assert dash.this_week == 1
    assert dash.days_since_last == 0


# ---------- симптом 2: days_since_last = -1 ----------


async def test_days_since_last_is_not_negative_west_of_utc(fresh_db, user_id):
    """UTC-3, тренировка в 23:00 местного времени: в UTC это уже следующий день,
    поэтому «сегодня» пользователя оказывалось раньше даты тренировки."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=-3)
    user = await db.get_user(user_id)
    await _session(db, user_id, "2026-07-24T02:00:00")  # 23 июля, 23:00 по местному
    today = timeutil.to_user_local(dt.datetime(2026, 7, 24, 2, 30), user).date()
    assert today == dt.date(2026, 7, 23)

    dates = [dt.date.fromisoformat(d) for d in await db.list_finished_workout_dates(user_id)]
    dash = analytics.compute_dashboard(dates, today)

    assert dash.days_since_last == 0
    assert dash.last_30_days == 1


def test_compute_dashboard_clamps_days_since_last_to_zero():
    """Дата тренировки может оказаться в будущем и без часовых поясов (импорт,
    ручная правка даты) — счётчик всё равно не должен показывать минус."""
    today = dt.date(2026, 7, 23)
    dash = analytics.compute_dashboard([today + dt.timedelta(days=1)], today)

    assert dash.days_since_last == 0


def test_thirty_day_window_includes_today():
    today = dt.date(2026, 7, 23)
    dash = analytics.compute_dashboard([today], today)

    assert dash.last_30_days == 1


# ---------- окна главного экрана ----------


async def test_weekly_volume_window_includes_tonight_workout(fresh_db, user_id):
    """Коридор объёма считается от местного «сегодня»: у UTC-3 вечерняя
    тренировка уходила в UTC-«завтра» и в панель не попадала."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=-3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-24T02:00:00", [(bench, 100, 5, 4)])

    today = dt.date(2026, 7, 23)
    window_start = today - dt.timedelta(days=analytics.VOLUME_WINDOW_DAYS - 1)
    counts = await db.weekly_volume_by_group(
        user_id, window_start.isoformat(), today.isoformat()
    )

    assert sum(counts.values()) == 4


async def test_daily_tonnage_keys_are_local_days(fresh_db, user_id):
    db = fresh_db
    await db.update_user(user_id, tz_offset=-3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-24T02:00:00", [(bench, 100, 5, 1)])

    daily = await db.daily_tonnage(user_id, "2026-07-17", "2026-07-23")

    assert daily == {"2026-07-23": 500.0}


async def test_top_exercises_window_uses_local_days(fresh_db, user_id):
    db = fresh_db
    await db.update_user(user_id, tz_offset=-3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-24T02:00:00", [(bench, 100, 5, 1)])
    await _session(db, user_id, "2026-07-20T18:00:00", [(bench, 100, 5, 1)])

    top = await db.top_exercises_by_frequency(user_id, "2026-06-01", "2026-07-23")

    assert [row["sessions"] for row in top] == [2]


async def test_e1rm_record_window_uses_local_days(fresh_db, user_id):
    """Рекорд, поставленный в первый день окна, должен в него попасть: у UTC+3
    начало окна — местная дата, а UTC-день ночной тренировки был на сутки
    раньше, и рекорд оказывался «до окна»."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-01T18:00:00", [(bench, 100, 5, 3)])
    await _session(db, user_id, "2026-07-17T22:00:00", [(bench, 110, 5, 3)])  # 18 июля местных

    assert await db.e1rm_record_count(user_id, "2026-07-18") == 1


async def test_last_session_by_group_is_a_local_day(fresh_db, user_id):
    """Восстановление считается разницей местных дат: UTC-день делал «дней с
    тренировки» отрицательным, и группа выглядела свежей на сутки раньше."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=-3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-24T02:00:00", [(bench, 100, 5, 4)])

    last = await db.last_session_by_group(user_id)

    assert [day for day, _ in last.values()] == ["2026-07-23"]


async def test_tonnage_since_counts_from_the_local_boundary(fresh_db, user_id):
    """`since` приходит местной датой, поэтому и started_at сравнивается по
    местному времени: иначе вечерняя тренировка воскресенья не попадала в
    недельный итог."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=3)
    bench = await _own(db, user_id, BENCH)
    await _session(db, user_id, "2026-07-19T22:00:00", [(bench, 100, 5, 1)])  # 20 июля местных

    assert await db.tonnage_since(user_id, "2026-07-20") == 500.0
    rollup = await db.weekly_exercise_rollup(user_id, "2026-07-20")
    assert [r["sets_count"] for r in rollup] == [1]


async def test_recent_programs_window_starts_at_a_local_day(fresh_db, user_id):
    """Окно «за последний месяц» тоже отсчитывается от местной даты."""
    db = fresh_db
    await db.update_user(user_id, tz_offset=3)
    routine_id = await db.create_routine(user_id, "Толкай", program_name="Сплит")
    workout_id = await db.create_workout(
        user_id, started_at="2026-06-23T22:00:00", routine_id=routine_id
    )  # 24 июня по местному времени
    await db.finish_workout(workout_id)

    recent = await db.list_recent_programs(user_id, "2026-06-24")

    assert [p["name"] for p in recent] == ["Сплит"]


async def test_ai_quota_day_follows_the_user_timezone(fresh_db, user_id):
    """Дневная квота на вопросы — это «сегодня» пользователя, а не сутки сервера:
    иначе счётчик обнулялся посреди его дня."""
    db = fresh_db
    utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    # Смещение, при котором местная дата гарантированно не равна UTC-дате.
    offset = 12 if utc_now.hour >= 12 else -12
    await db.update_user(user_id, tz_offset=offset)
    local_day = (utc_now + dt.timedelta(hours=offset)).date().isoformat()
    assert local_day != utc_now.date().isoformat()

    await db.increment_ai_question_count(user_id)
    await db.increment_ai_search_count(user_id)

    cur = await db.conn().execute(
        "SELECT date FROM ai_question_usage WHERE telegram_id = ?", (user_id,)
    )
    assert [r["date"] for r in await cur.fetchall()] == [local_day]
    assert await db.get_ai_question_count_today(user_id) == 1
    assert await db.get_ai_search_count_today(user_id) == 1
