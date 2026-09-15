"""RPE в расчёте e1RM: @9 — это подход, у которого остался ещё один повтор.

До этого RPE записывался и показывался, но в арифметику не заходил, и два дня
из реального дневника выглядели так:

    15.09: 115×6 @9, 110×8 @9, 110×6 @8 → e1RM 139.3
    11.09: 110×8,    110×7,    110×6    → e1RM 139.3

Один и тот же топовый подход 110×8, одинаковая цифра, плато на экране — при том
что 15-го та же работа сделана с запасом и сверху добавлены 115×6. Теперь запас
повторов (RIR = 10 − RPE) уходит в формулу, и 15-е читается прогрессом.
"""
import datetime as dt

import pytest

import analytics
import db
import formatting
import view_builder
from analytics import SessionStats, SetRow

# ---------- формула ----------


def test_rpe_nine_counts_as_one_more_rep():
    assert analytics.e1rm(110, 8, "epley", 9) == pytest.approx(analytics.e1rm(110, 9))


def test_rpe_eight_counts_as_two_more_reps():
    assert analytics.e1rm(110, 6, "epley", 8) == pytest.approx(analytics.e1rm(110, 8))


def test_rpe_ten_is_failure_and_changes_nothing():
    assert analytics.e1rm(110, 8, "epley", 10) == pytest.approx(analytics.e1rm(110, 8))


def test_missing_rpe_keeps_the_old_number():
    """Половина истории без RPE, и она обязана считаться ровно как раньше —
    иначе смена формулы переписала бы задним числом все рекорды."""
    assert analytics.e1rm(110, 8, "epley", None) == pytest.approx(139.33, abs=0.01)


def test_half_point_rpe_is_half_a_rep():
    assert analytics.e1rm(100, 5, "epley", 9.5) == pytest.approx(100 * (1 + 5.5 / 30))


def test_warmup_rpe_cannot_invent_a_record():
    """Парсер пускает любой RPE из (0, 10]: @2 на разминке дало бы +8 повторов и
    e1RM выше рабочего максимума. Запас режется по RIR_CAP."""
    assert analytics.e1rm(20, 3, "epley", 2) == pytest.approx(
        analytics.e1rm(20, 3, "epley", 5)
    )
    assert analytics.reps_in_reserve(2) == analytics.RIR_CAP


def test_single_with_reserve_beats_the_bar_itself():
    """reps<=1 отдаёт вес как есть, но 100×1 @9 — это не максимум: один повтор
    в запасе есть, и оценка обязана быть выше ста."""
    assert analytics.e1rm(100, 1, "epley", 9) > 100


def test_brzycki_also_reads_the_reserve():
    assert analytics.e1rm(110, 6, "brzycki", 8) == pytest.approx(
        analytics.e1rm(110, 8, "brzycki")
    )


# ---------- выбор топового подхода ----------


def _session(sets):
    return SessionStats(workout_id=1, started_at="2026-09-15T10:00:00", sets=sets)


def test_two_days_of_the_same_top_set_stop_looking_like_a_plateau():
    sep15 = _session([
        SetRow(115, 6, 1, "2026-09-15T10:00:00", 9),
        SetRow(110, 8, 1, "2026-09-15T10:00:00", 9),
        SetRow(110, 6, 1, "2026-09-15T10:00:00", 8),
    ])
    sep11 = SessionStats(
        workout_id=2,
        started_at="2026-09-11T10:00:00",
        sets=[
            SetRow(110, 8, 2, "2026-09-11T10:00:00"),
            SetRow(110, 7, 2, "2026-09-11T10:00:00"),
            SetRow(110, 6, 2, "2026-09-11T10:00:00"),
        ],
    )

    assert round(sep11.top_e1rm, 1) == 139.3
    assert round(sep15.top_e1rm, 1) == 143.0


def test_reserve_can_move_which_set_is_the_top_one():
    """115×6 @9 — это 115×7, и он обходит отказные 110×8, хотя по записанным
    повторам проигрывал."""
    session = _session([
        SetRow(110, 8, 1, "2026-09-15T10:00:00", 10),
        SetRow(115, 6, 1, "2026-09-15T10:00:00", 9),
    ])
    assert session.top_set.weight == 115


def test_personal_records_read_the_reserve():
    pr = analytics.compute_personal_records([
        _session([SetRow(110, 8, 1, "2026-09-15T10:00:00", 9)])
    ])
    assert round(pr.max_e1rm, 1) == 143.0


def test_gold_book_reads_the_reserve():
    book = analytics.gold_book([
        _session([SetRow(110, 8, 1, "2026-09-15T10:00:00", 9)])
    ])
    assert round(book.best_e1rm, 1) == 143.0


# ---------- наклон по сессиям RPE не трогает ----------


def test_stall_trend_ignores_rpe_wobble():
    """Полбалла самооценки на неизменных 140×5 не имеет права превратиться в
    «регрессируешь»: у RPE в вердикте свой канал (avg_top_rpe), а наклон
    считается по записанным повторам. См. SessionStats.top_e1rm_ignoring_rpe."""
    today = dt.date(2026, 9, 15)
    sessions = []
    for i, rpe in enumerate([9.5, 10, 9.5, 10]):
        day = (today - dt.timedelta(weeks=4 - i)).isoformat() + "T10:00:00"
        sessions.append(
            SessionStats(workout_id=i, started_at=day, sets=[SetRow(140, 5, i, day, rpe)])
        )

    verdict = analytics.classify_stall(sessions, today)

    assert verdict.kind == "dead_end"
    assert verdict.e1rm_slope_per_week == 0.0


# ---------- SQL-зеркало ----------


async def _exercise(database, user_id: int):
    gid = await database.create_muscle_group(user_id, "Грудь")
    return await database.create_exercise(user_id, "Жим лёжа", gid)


@pytest.mark.asyncio
async def test_sql_e1rm_matches_python_with_rpe(fresh_db, user_id):
    """db._e1rm_sql — второй экземпляр той же формулы, и он ставит планку для 🥇,
    которую проверяет Python. Разойдутся — один подход даст два ответа."""
    ex_id = await _exercise(fresh_db, user_id)
    workout_id = await fresh_db.create_workout(user_id, started_at="2026-09-11T10:00:00")
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    await fresh_db.append_set(block_id, ex_id, 0, 110.0, 8, 9)
    await fresh_db.finish_workout(workout_id, finished_at="2026-09-11T10:00:00")

    for formula in ("epley", "brzycki"):
        from_sql = await db.max_e1rm_before_workout(user_id, ex_id, 0, formula)
        assert from_sql == pytest.approx(analytics.e1rm(110, 8, formula, 9))


@pytest.mark.asyncio
async def test_gold_mark_goes_to_the_set_that_only_wins_on_rpe(fresh_db, user_id):
    """История: 115×6 в отказ. Сегодня те же 115×6, но с повтором в запасе —
    это работа тяжелее, и 🥇 обязано приехать."""
    ex_id = await _exercise(fresh_db, user_id)
    old = await fresh_db.create_workout(user_id, started_at="2026-09-11T10:00:00")
    old_block = await fresh_db.create_block(old, "single")
    await fresh_db.add_block_exercise(old_block, ex_id, 0)
    await fresh_db.append_set(old_block, ex_id, 0, 115.0, 6)
    await fresh_db.finish_workout(old, finished_at="2026-09-11T10:00:00")

    live = await fresh_db.create_workout(user_id, started_at="2026-09-15T10:00:00")
    live_block = await fresh_db.create_block(live, "single")
    await fresh_db.add_block_exercise(live_block, ex_id, 0)
    await fresh_db.append_set(live_block, ex_id, 0, 115.0, 6, 9)

    blocks = await view_builder.build_block_views(live, "epley", mark_golds=True)

    assert blocks[0].gold_index == 0
    assert "115×6 @9 🥇" in formatting.build_live_session_text(
        blocks, active_exercise_id=ex_id
    )
