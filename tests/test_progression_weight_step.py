"""The "🎯 Цель" weight bump is read off the exercise's own history rather than a
fixed 2.5kg constant — this wires analytics.infer_weight_step to real logged sets
through _exercise_history, complementing the pure tests in test_analytics.py."""
import datetime as dt

import pytest

from handlers import workout


async def _log_session(db, user_id, ex_id, day_offset, sets):
    day = dt.date.today() - dt.timedelta(days=day_offset)
    wid = await db.create_workout(user_id, started_at=f"{day.isoformat()}T12:00:00")
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(block_id, ex_id, i, 0, weight, reps, None)
    await db.finish_workout(wid, finished_at=f"{day.isoformat()}T12:30:00")


async def _exercise(db, user_id, name="Жим гантелей"):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    return await db.create_exercise(user_id, name, group_id)


@pytest.mark.asyncio
async def test_step_inferred_from_dumbbell_history(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id)
    await _log_session(db, user_id, ex_id, 14, [(20.0, 10)])
    await _log_session(db, user_id, ex_id, 7, [(22.0, 10)])
    await _log_session(db, user_id, ex_id, 1, [(24.0, 10)])

    last_session, step = await workout._exercise_history(ex_id)

    assert last_session == [(24.0, 10, None)]
    assert step == pytest.approx(2.0)
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 26×5" in hint  # a dumbbell that exists, not 26.5


@pytest.mark.asyncio
async def test_backoff_sets_do_not_become_the_step(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Жим лёжа")
    await _log_session(db, user_id, ex_id, 1, [(100.0, 10), (80.0, 10)])

    last_session, step = await workout._exercise_history(ex_id)

    assert step == pytest.approx(20.0)  # inferred, but too coarse to be believed
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 102.5×5" in hint


@pytest.mark.asyncio
async def test_first_ever_session_falls_back_to_the_default_step(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Тяга блока")
    await _log_session(db, user_id, ex_id, 1, [(50.0, 10)])

    last_session, step = await workout._exercise_history(ex_id)

    assert step is None
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 52.5×5" in hint


@pytest.mark.asyncio
async def test_heavy_lift_without_history_jumps_by_five(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Становая")
    await _log_session(db, user_id, ex_id, 1, [(210.0, 10)])

    last_session, step = await workout._exercise_history(ex_id)

    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 215×5" in hint
