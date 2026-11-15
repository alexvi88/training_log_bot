"""End-to-end: build_daily_push wired against a real (in-memory) DB."""

import datetime as dt

import pytest

import engagement
import push_texts

pytestmark = pytest.mark.asyncio


async def test_build_daily_push_fires_the_matching_skip_milestone(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )

    today = dt.date(2026, 7, 13)  # 7 full empty days after the workout (today not counted)
    decision = await engagement.build_daily_push(user_id, today)

    assert decision is not None
    assert decision.category == push_texts.SKIP_7
    assert "АТЛЕТ" in decision.text
    # a day-7 skip must never draw a day-14 line ("две недели"/"четырнадцать")
    assert "недел" in decision.text.lower()
    assert "четырнадцат" not in decision.text.lower()


async def test_build_daily_push_respects_one_per_day_dedup(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-05T10:00:00", finished_at="2026-07-05T11:00:00"
    )
    today = dt.date(2026, 7, 13)
    await db.record_push(user_id, push_texts.SKIP_7, "already sent today", today.isoformat())

    decision = await engagement.build_daily_push(user_id, today)
    assert decision is None


async def test_build_daily_push_two_days_out_is_silent(fresh_db, user_id):
    db = fresh_db
    await db.create_finished_workout(
        user_id, started_at="2026-07-10T10:00:00", finished_at="2026-07-10T11:00:00"
    )
    today = dt.date(2026, 7, 12)  # 2 days later — below the first milestone (3)

    decision = await engagement.build_daily_push(user_id, today)
    assert decision is None


async def test_build_daily_push_none_for_user_without_workouts(fresh_db, user_id):
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 12))
    assert decision is None


async def test_build_newbie_push_fires_day_after_signup(fresh_db, user_id):
    decision = await engagement.build_newbie_push(user_id, "2026-07-11T09:00:00", dt.date(2026, 7, 12))
    assert decision is not None
    assert decision.category == push_texts.NEWBIE_NUDGE
    assert "АТЛЕТ" in decision.text


async def test_build_newbie_push_silent_same_day_as_signup(fresh_db, user_id):
    decision = await engagement.build_newbie_push(user_id, "2026-07-12T09:00:00", dt.date(2026, 7, 12))
    assert decision is None


async def test_build_newbie_push_respects_one_per_day_dedup(fresh_db, user_id):
    db = fresh_db
    today = dt.date(2026, 7, 12)
    await db.record_push(user_id, push_texts.NEWBIE_NUDGE, "already sent today", today.isoformat())

    decision = await engagement.build_newbie_push(user_id, "2026-07-11T09:00:00", today)
    assert decision is None


async def test_monday_streak_milestone_beats_the_skip_jab(fresh_db, user_id):
    """Four consecutive training weeks, checked on the following Monday: the
    celebration must win over the skip-5 jab that fires the same day."""
    db = fresh_db
    for day in ("2026-06-17", "2026-06-24", "2026-07-01", "2026-07-08"):  # 4 weeks in a row
        await db.create_finished_workout(
            user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
        )

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 13))  # Monday, 5 days out

    assert decision is not None
    assert decision.category == push_texts.STREAK_MILESTONE
    assert "4 недели" in decision.text


async def _seed_heavy_workout(db, ex_id, user_id, day: str):
    workout_id = await db.create_finished_workout(
        user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
    )
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, 100.0, 8)
    await db.add_set(block, ex_id, 2, 0, 100.0, 8)
    await db.add_set(block, ex_id, 3, 0, 100.0, 8)


async def test_friday_rank_near_push_names_the_rank_and_the_gap(fresh_db, user_id):
    """13 workouts, ~31 т, 2+ раза в неделю: до «Работяги» (15 тренировок)
    ровно две — в пятницу приходит пуш с целью."""
    db = fresh_db
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    for k in range(13):
        day = (dt.date(2026, 7, 16) - dt.timedelta(days=2 * k)).isoformat()
        await _seed_heavy_workout(db, ex_id, user_id, day)

    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 17))  # Friday, 1 day out

    assert decision is not None
    assert decision.category == push_texts.RANK_NEAR
    assert "Работяга" in decision.text
    assert "2 тренировки" in decision.text


async def _seed_recent_workout(db, user_id):
    # Saturday 2026-07-18; on Sunday 19th / Monday 20th → tonnage>0, day-since-last not a milestone.
    await db.create_finished_workout(
        user_id, started_at="2026-07-18T10:00:00", finished_at="2026-07-18T11:00:00"
    )
    block = await db.create_block(
        (await db.list_workouts(user_id))[0]["id"], "single"
    )
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, "Жим", gid)
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, 100.0, 8)


async def _workout_with_set(db, user_id, day: str, weight: float, reps: int = 10) -> None:
    wid = await db.create_finished_workout(
        user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
    )
    block = await db.create_block(wid, "single")
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await db.create_exercise(user_id, f"Жим {day}", gid)
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, weight, reps)


MONDAY = dt.date(2026, 7, 20)
SUNDAY = dt.date(2026, 7, 19)


async def test_monday_digest_uses_ai_when_available(fresh_db, user_id, monkeypatch):
    await _seed_recent_workout(fresh_db, user_id)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)
    asked = []

    async def fake_digest(uid, week_start=None):
        asked.append(week_start)
        return "ПРИВЕТ АТЛЕТ! Неделя выдалась крепкой."

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", fake_digest)

    decision = await engagement.build_weekly_digest_push(user_id, MONDAY)
    assert decision is not None
    assert decision.category == push_texts.AI_WEEKLY
    assert decision.text.startswith("ПРИВЕТ АТЛЕТ")
    # Модели передана прошлая неделя явно — её понедельник.
    assert asked == [dt.date(2026, 7, 13)]


async def test_monday_digest_falls_back_to_static_when_ai_returns_none(fresh_db, user_id, monkeypatch):
    await _seed_recent_workout(fresh_db, user_id)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)

    async def no_digest(uid, week_start=None):
        return None

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", no_digest)

    decision = await engagement.build_weekly_digest_push(user_id, MONDAY)
    assert decision is not None
    assert decision.category == push_texts.WEEKLY_DIGEST


async def test_monday_digest_describes_last_week(fresh_db, user_id, monkeypatch):
    """Утром понедельника dashboard.this_week = 0, а пуш про прошлую неделю:
    число тренировок — за пн–вс прошлой, тоннаж «за месяц» — 30 суток по
    вчера, ключ недели — её понедельник."""
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    # Прошлая неделя 13–19 июля: три тренировки по 1т (100кг × 10).
    for day in ("2026-07-13", "2026-07-15", "2026-07-18"):
        await _workout_with_set(fresh_db, user_id, day, 100.0)
    # Неделей раньше — одна, в 30-дневное окно (20 июня – 19 июля) входит.
    await _workout_with_set(fresh_db, user_id, "2026-07-08", 100.0)
    # Первый день окна — в месяц входит, день до него — нет.
    await _workout_with_set(fresh_db, user_id, "2026-06-20", 50.0)
    await _workout_with_set(fresh_db, user_id, "2026-06-19", 500.0)
    # Сегодняшняя (понедельник, рано утром) — уже новая неделя: не в счёт.
    await _workout_with_set(fresh_db, user_id, "2026-07-20", 300.0)

    decision = await engagement.build_weekly_digest_push(user_id, MONDAY)
    assert decision is not None and decision.category == push_texts.WEEKLY_DIGEST
    assert decision.ios_params == {"tonnage": "4.5т", "week_count": "3 тренировки"}
    assert decision.ios_route_params == {"week": "2026-07-13"}


async def test_monday_ai_digest_is_saved_under_last_weeks_key(fresh_db, user_id, monkeypatch):
    await _seed_recent_workout(fresh_db, user_id)
    monkeypatch.setattr(engagement.ai_trainer, "is_configured", lambda: True)

    async def fake_digest(uid, week_start=None):
        return "ПРИВЕТ АТЛЕТ! Прошлая неделя удалась."

    monkeypatch.setattr(engagement.ai_trainer, "weekly_digest", fake_digest)
    decision = await engagement.build_weekly_digest_push(user_id, MONDAY)
    assert decision.ios_route_params == {"week": "2026-07-13"}
    assert (await fresh_db.get_weekly_digest(user_id, "2026-07-13"))["text"] == "ПРИВЕТ АТЛЕТ! Прошлая неделя удалась."
    assert await fresh_db.get_weekly_digest(user_id, "2026-07-20") is None


async def test_digest_only_on_monday(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    await _seed_recent_workout(fresh_db, user_id)
    for day in range(14, 20):  # вт … вс
        assert await engagement.build_weekly_digest_push(user_id, dt.date(2026, 7, day)) is None
    assert await engagement.build_weekly_digest_push(user_id, MONDAY) is not None


async def test_sunday_evening_has_no_digest(fresh_db, user_id, monkeypatch):
    """Дайджест ушёл с воскресенья: вечерний слот воскресенья без плато молчит,
    а не шлёт итоги недели, которая ещё идёт."""
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    await _seed_recent_workout(fresh_db, user_id)
    assert await engagement.build_daily_push(user_id, SUNDAY) is None


async def test_sunday_evening_keeps_the_plateau(fresh_db, user_id):
    gid = (await fresh_db.list_muscle_groups(None, global_only=True))[0]["id"]
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", gid)
    for day in ("2026-07-13", "2026-07-15", "2026-07-17"):
        wid = await fresh_db.create_finished_workout(
            user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
        )
        block = await fresh_db.create_block(wid, "single")
        await fresh_db.add_block_exercise(block, ex_id, 0)
        await fresh_db.add_set(block, ex_id, 1, 0, 60.0, 12)

    decision = await engagement.build_daily_push(user_id, SUNDAY)
    assert decision is not None and decision.category == push_texts.PLATEAU
    # Плато — только воскресенье: в понедельник вечером его нет.
    assert await engagement.build_daily_push(user_id, MONDAY) is None


async def test_monday_evening_is_quiet_after_the_morning_digest(fresh_db, user_id, monkeypatch):
    """Один пуш в день: ушёл утренний дайджест — вечерний слот понедельника молчит."""
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    await fresh_db.create_finished_workout(
        user_id, started_at="2026-07-06T10:00:00", finished_at="2026-07-06T11:00:00"
    )
    await _workout_with_set(fresh_db, user_id, "2026-07-13", 100.0)
    # 17 июля → 20 июля: два целых пустых дня, пропуска нет; дайджест есть.
    await _workout_with_set(fresh_db, user_id, "2026-07-17", 100.0)
    decision = await engagement.build_weekly_digest_push(user_id, MONDAY)
    assert decision is not None
    await fresh_db.record_push(user_id, decision.category, decision.text, MONDAY.isoformat())
    assert await engagement.build_daily_push(user_id, MONDAY) is None


async def test_digest_yields_to_the_monday_streak_milestone(fresh_db, user_id, monkeypatch):
    """Рубеж серии бывает только по понедельникам. Дайджест его не съедает:
    утром уступает, вечером уходит рубеж."""
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    # Четыре недели подряд, по тренировке в среду: 24 июня … 15 июля, плюс суббота 18-го.
    for day in ("2026-06-24", "2026-07-01", "2026-07-08", "2026-07-15", "2026-07-18"):
        await _workout_with_set(fresh_db, user_id, day, 100.0)
    assert await engagement.build_weekly_digest_push(user_id, MONDAY) is None
    decision = await engagement.build_daily_push(user_id, MONDAY)
    assert decision is not None and decision.category == push_texts.STREAK_MILESTONE


async def test_digest_yields_to_a_skip_milestone(fresh_db, user_id, monkeypatch):
    """«Неделя без зала» важнее «на прошлой неделе — 0 тренировок» — как и в
    воскресной схеме, где пропуск стоял выше дайджеста."""
    monkeypatch.setattr(engagement.config, "AI_WEEKLY_DIGEST_ENABLED", False)
    # 12 июля → 20 июля: семь целых пустых дней (сегодняшний не в счёте).
    await _workout_with_set(fresh_db, user_id, "2026-07-12", 100.0)
    real_pick = push_texts.pick_text
    picked = []

    async def counting_pick(telegram_id, category, **params):
        picked.append(category)
        return await real_pick(telegram_id, category, **params)

    monkeypatch.setattr(push_texts, "pick_text", counting_pick)
    assert await engagement.build_weekly_digest_push(user_id, MONDAY) is None
    # Утренний вопрос «уступить ли?» мешок ротации пропуска не трогает.
    assert picked == []
    decision = await engagement.build_daily_push(user_id, MONDAY)
    assert decision is not None and decision.category == push_texts.SKIP_7


async def _every_other_day_until_sunday(db, user_id):
    """Шесть тренировок через день, последняя — в воскресенье 5 июля."""
    for day in (25, 27, 29):
        await db.create_finished_workout(
            user_id, started_at=f"2026-06-{day}T10:00:00", finished_at=f"2026-06-{day}T11:00:00"
        )
    for day in (1, 3, 5):
        await db.create_finished_workout(
            user_id, started_at=f"2026-07-0{day}T10:00:00", finished_at=f"2026-07-0{day}T11:00:00"
        )


async def test_skip_3_waits_for_the_third_full_empty_day(fresh_db, user_id):
    """Тренировка в воскресенье, пн–вт отдых: в среду вечером «третий день без
    зала» — неправда (он может быть в зале), подколка — в четверг."""
    await _every_other_day_until_sunday(fresh_db, user_id)
    assert await engagement.build_daily_push(user_id, dt.date(2026, 7, 8)) is None
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 9))
    assert decision is not None and decision.category == push_texts.SKIP_3


async def test_no_skip_push_while_a_workout_is_running(fresh_db, user_id, monkeypatch):
    await _every_other_day_until_sunday(fresh_db, user_id)
    await fresh_db.create_workout(user_id, started_at="2026-07-09T17:30:00")
    now = dt.datetime(2026, 7, 9, 18, 0)
    monkeypatch.setattr(engagement, "_utc_now", lambda: now)
    assert await engagement.build_daily_push(user_id, dt.date(2026, 7, 9)) is None
    # брошенная неделю назад тренировка подколку не глушит
    monkeypatch.setattr(engagement, "_utc_now", lambda: now + dt.timedelta(days=7))
    decision = await engagement.build_daily_push(user_id, dt.date(2026, 7, 9))
    assert decision is not None and decision.category == push_texts.SKIP_3


async def test_plateau_ignores_reps_only_sets_carried_by_bodyweight(fresh_db, user_id):
    """Подтягивания «×13» три тренировки подряд при записанном весе тела:
    нагрузка у подходов 81.5, но вес атлет не вводил — «вес засиделся, пора
    добавить» им не приходит. Штанга на том же весе — приходит."""
    db = fresh_db
    gid = (await db.list_muscle_groups(None, global_only=True))[0]["id"]
    pull = await db.create_exercise(user_id, "Подтягивания на кольцах", gid)
    await db.set_exercise_bodyweight(pull, True)
    await db.add_bodyweight_log(user_id, 81.5)
    for day in ("2026-07-10", "2026-07-12", "2026-07-14"):
        wid = await db.create_finished_workout(
            user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
        )
        block = await db.create_block(wid, "single")
        await db.add_block_exercise(block, pull, 0)
        await db.add_set(block, pull, 1, 0, 0.0, 13)
    assert await engagement._find_plateau_exercise(user_id) is None

    bench = await db.create_exercise(user_id, "Жим", gid)
    for day in ("2026-07-11", "2026-07-13", "2026-07-15"):
        wid = await db.create_finished_workout(
            user_id, started_at=f"{day}T10:00:00", finished_at=f"{day}T11:00:00"
        )
        block = await db.create_block(wid, "single")
        await db.add_block_exercise(block, bench, 0)
        await db.add_set(block, bench, 1, 0, 60.0, 13)
    assert await engagement._find_plateau_exercise(user_id) == (bench, "Жим")
