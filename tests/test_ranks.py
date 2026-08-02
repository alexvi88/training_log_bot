"""Звания: линейная лестница от результатов × частоты.

Звание растёт по слабейшей из двух осей — накопленное (тренировки и тоннаж) и
текущий темп. Перерыв стоит ровно одной ступени, а не всей лестницы.
"""
import datetime as dt

import pytest

import analytics
import formatting

pytestmark = pytest.mark.asyncio


async def test_ladder_is_linear_and_starts_at_the_bottom():
    levels = [r.level for r in analytics.RANKS]
    assert levels == sorted(levels) == list(range(len(analytics.RANKS)))
    # Каждая ступень строго требовательнее предыдущей по всем трём осям.
    for lower, higher in zip(analytics.RANKS, analytics.RANKS[1:], strict=False):
        assert higher.min_workouts > lower.min_workouts
        assert higher.min_tonnage_kg > lower.min_tonnage_kg
        assert higher.min_per_week >= lower.min_per_week
    assert analytics.rank_for(0, 0, 0.0).level == 0


async def test_rank_is_capped_by_the_weaker_axis():
    """Тоннаж без регулярности — прошлые заслуги; регулярность без объёма —
    разминка. Звание берёт минимум."""
    heavy_but_rare = analytics.rank_for(60, 80_000, 1.5)
    frequent_but_light = analytics.rank_for(8, 6_000, 3.0)

    assert heavy_but_rare.name == "Станок"
    assert frequent_but_light.name == "Салага"  # частота высокая, но объёма нет


async def test_a_break_costs_one_rung_not_the_whole_ladder():
    """Ветеран с пятью сотнями тренировок за месяц простоя не должен
    становиться «Новичком» — это читается как сломанный счётчик."""
    training = analytics.rank_for(500, 2_000_000, 3.0)
    on_a_break = analytics.rank_for(500, 2_000_000, 0.0)

    assert training.name == "Дед зала"
    assert on_a_break.level == training.level - 1
    assert on_a_break.name == "Ветеран подвала"


async def test_returning_to_the_pace_restores_the_rank():
    assert analytics.rank_for(500, 2_000_000, 0.0).level == 5
    assert analytics.rank_for(500, 2_000_000, 3.0).level == 6


async def test_frequency_counts_only_the_recent_window():
    today = dt.date(2026, 8, 2)
    long_ago = [today - dt.timedelta(weeks=30) + dt.timedelta(days=i) for i in range(40)]
    recent = [today - dt.timedelta(days=i * 3) for i in range(8)]

    assert analytics.workouts_per_week(long_ago, today) == 0.0
    assert analytics.workouts_per_week(recent, today) == pytest.approx(1.0)


async def test_future_dated_workouts_do_not_inflate_frequency():
    today = dt.date(2026, 8, 2)
    assert analytics.workouts_per_week([today + dt.timedelta(days=5)], today) == 0.0


async def test_gap_names_the_single_worst_axis():
    """Одна причина — это цель; три строки с недостачами читаются как отказ."""
    rank = analytics.rank_for(60, 80_000, 2.0)
    gap = analytics.rank_gap(rank, 60, 80_000, 2.0)

    assert gap is not None
    assert gap.count("ещё") + gap.count("держать") == 1


async def test_top_rank_has_no_gap():
    top = analytics.RANKS[-1]
    assert analytics.next_rank(top) is None
    assert analytics.rank_gap(top, 10_000, 10_000_000, 10.0) is None


async def test_rank_lines_render():
    rank = analytics.rank_for(60, 80_000, 2.0)
    line = formatting.format_rank_line(rank, "ещё 120.0 т")
    assert "Станок" in line and "до следующего" in line
    assert "до следующего" not in formatting.format_rank_line(rank)
    assert "Новое звание" in formatting.format_rank_promotion(rank)


# ---------- объявление на карточке ----------


async def _finished(db, user_id: int, when: dt.date, sets):
    workout_id = await db.create_workout(user_id, started_at=f"{when.isoformat()}T10:00:00")
    gid = await db.create_muscle_group(user_id, f"Г{when.isoformat()}")
    ex_id = await db.create_exercise(user_id, f"Упр {when.isoformat()}", gid)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for weight, reps in sets:
        await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=f"{when.isoformat()}T11:00:00")


async def test_promotion_is_announced_once_then_stays_quiet(fresh_db, user_id):
    from handlers import workout as workout_handlers

    db = fresh_db
    today = dt.date.today()
    for i in range(6):
        await _finished(db, user_id, today - dt.timedelta(days=i * 3), [(100.0, 10)] * 6)

    user = await db.get_user(user_id)
    first = await workout_handlers._rank_promotion(user_id, user)
    assert first is not None and first.level > 0

    user = await db.get_user(user_id)
    assert await workout_handlers._rank_promotion(user_id, user) is None


async def test_demotion_is_not_announced(fresh_db, user_id):
    """Понижение молча опускает отметку — карточка завершения не место, чтобы
    сообщать человеку, что он сдал."""
    from handlers import workout as workout_handlers

    db = fresh_db
    await db.update_user(user_id, rank_level_seen=5)
    user = await db.get_user(user_id)

    assert await workout_handlers._rank_promotion(user_id, user) is None
    assert (await db.get_user(user_id))["rank_level_seen"] == 0  # отметка опущена


async def test_hall_of_fame_shows_the_rank_first(fresh_db, user_id):
    from handlers.history import build_hall_of_fame_text

    db = fresh_db
    today = dt.date.today()
    for i in range(6):
        await _finished(db, user_id, today - dt.timedelta(days=i * 3), [(100.0, 10)] * 6)

    text = await build_hall_of_fame_text(user_id)

    assert "Звание:" in text
    assert text.index("Звание:") < text.index("Всего тренировок")
