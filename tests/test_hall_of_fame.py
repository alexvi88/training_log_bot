"""Hall of Fame: analytics.max_week_streak, db aggregates, and the built screen."""
import datetime as dt

import pytest

import analytics
import formatting
import hall_of_fame_data
from handlers import history


def _home(total_workouts=42, tonnage_kg=125000, tonnage_equivalent=None, best_week_streak=6,
          lifts=(), main=True, unit="kg", earned=frozenset(), recent=()):
    """Главный экран из готовых чисел — как его собирает хендлер."""
    hof = hall_of_fame_data.HallOfFame(
        total_workouts=total_workouts, tonnage_kg=tonnage_kg, tonnage_equivalent=tonnage_equivalent,
        best_week_streak=best_week_streak, longest_workout_seconds=5400, unit=unit,
    )
    headline = hall_of_fame_data.HeadlineLifts(main=main, lifts=list(lifts))
    return formatting.build_achievements_home(hof, headline, set(earned), list(recent))


def test_max_week_streak_finds_best_run_anywhere():
    d = dt.date(2026, 1, 5)  # a Monday
    # weeks 0,1,2 (a 3-run), gap, then weeks 5,6 (a 2-run)
    dates = [d, d + dt.timedelta(weeks=1), d + dt.timedelta(weeks=2),
             d + dt.timedelta(weeks=5), d + dt.timedelta(weeks=6)]
    assert analytics.max_week_streak(dates) == 3


def test_max_week_streak_empty():
    assert analytics.max_week_streak([]) == 0


def test_hall_of_fame_screen_empty():
    text = _home(total_workouts=0, tonnage_kg=0, best_week_streak=0)
    assert "Пока пусто" in text


def test_hall_of_fame_screen_populated():
    text = _home(
        tonnage_equivalent="Это как 25 слонов 🐘.",
        lifts=[("Жим штанги лёжа", 120.0, 3, 132.0)],
    )
    assert "<b>42</b> тренировки" in text
    assert "125 тонн" in text
    assert "6</b> недель" in text
    assert "Жим штанги лёжа — e1RM <b>132кг</b> (120×3)" in text
    assert "слонов" in text
    # Самая длинная тренировка с главного экрана ушла.
    assert "1 ч 30 мин" not in text


@pytest.mark.asyncio
async def test_aggregates_and_build_text(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    wid = await db.create_workout(user_id)
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, bench, 0)
    await db.add_set(block_id, bench, 0, 0, 100.0, 5, None)
    await db.add_set(block_id, bench, 1, 0, 100.0, 5, None)
    await db.finish_workout(wid, None)

    agg = await db.hall_of_fame_aggregates(user_id)
    assert agg["tonnage"] == 1000.0  # 100*5 + 100*5
    assert agg["sets_count"] == 2
    assert await db.max_weight_ever(user_id) == 100.0
    assert await db.count_distinct_exercises_used(user_id) == 1

    text = await history.build_achievements_home_text(user_id)
    # Своё «Жим лёжа» не из каталога — не базовое: идёт в «самые частые».
    assert "Рекорды в самых частых упражнениях" in text
    assert "Жим лёжа — <b>100×5</b>" in text
    assert "1 тонна" in text


@pytest.mark.asyncio
async def test_longest_workout_ignores_wall_clock_idle_time(fresh_db, user_id):
    """Живой прогон: экран Достижений показывал «Самая длинная тренировка:
    9 ч 51 мин», а «🔒 Марафонец — Тренировка длиннее 2 часов» рядом же
    оставался запертым — прямая нестыковка. Причина была в том, что
    db.hall_of_fame_aggregates мерил сырой started_at→finished_at (человек
    оставил тренировку открытой на часы, а не тренировался всё это время),
    а Марафонец — честный интервал между первым и последним подходом
    (view_builder.workout_duration_seconds). Число на экране обязано быть
    тем же самым, что решает достижение."""
    import view_builder

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    wid = await db.create_workout(user_id, started_at="2026-08-08T09:00:00")
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, bench, 0)
    # Оба подхода реально сделаны за 5 минут — тренировка короткая.
    await db.append_set(block_id, bench, 0, 0, 100.0, 5)
    await db.append_set(block_id, bench, 1, 0, 100.0, 5)
    # Но закрыл её человек только вечером — часы простоя, не тренировки.
    await db.finish_workout(wid, finished_at="2026-08-08T18:51:00")

    agg = await db.hall_of_fame_aggregates(user_id)
    assert "longest_workout_seconds" not in agg

    longest = await view_builder.longest_workout_seconds(user_id)
    assert longest < 3600, "простой между стартом и финишем не должен считаться тренировкой"


def test_all_records_page_is_not_folded_or_trimmed():
    lifts = [(f"Упражнение {i}", 100.0 + i, 5, 130.0 - i) for i in range(8)]
    text, pages = formatting.build_records_page(lifts)
    assert pages == 1
    assert "blockquote" not in text
    assert text.count("• ") == len(lifts)


def test_all_records_long_list_goes_to_pages_instead_of_being_cut():
    lifts = [(f"Очень длинное название упражнения номер {i}", 100.0, 5, 130.0) for i in range(200)]
    seen = 0
    text, pages = formatting.build_records_page(lifts, page=0)
    for page in range(pages):
        text, _ = formatting.build_records_page(lifts, page=page)
        assert formatting.telegram_length(text) <= formatting.MESSAGE_LIMIT
        seen += text.count("• ")
    assert seen == len(lifts), "ни один рекорд не теряется"
    # Страница за краем прижимается к последней.
    assert formatting.build_records_page(lifts, page=999)[0] == text


def test_bodyweight_record_counts_reps():
    text, _ = formatting.build_records_page([("Подтягивания", 0.0, 15, 0.0)])
    assert "Подтягивания — 15 повторов" in text
    assert "e1RM" not in text
    home = _home(lifts=[("Подтягивания", 0.0, 15, 0.0)], main=False)
    assert "Подтягивания — 15 повторов" in home


def test_most_trained_fallback_has_no_e1rm():
    text = _home(lifts=[("Жим в тренажёре", 60.0, 12, 84.0)], main=False)
    assert "Жим в тренажёре — <b>60×12</b>" in text
    assert "e1RM" not in text
