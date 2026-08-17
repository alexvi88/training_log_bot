"""The "🎯 Цель" weight bump is read off the exercise's own history rather than a
fixed 2.5kg constant — this wires analytics.infer_weight_step to real logged sets
through _exercise_history, complementing the pure tests in test_analytics.py."""
import datetime as dt
import json

import pytest

import analytics
import formatting
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
    await _log_session(db, user_id, ex_id, 14, [(20.0, 12)])
    await _log_session(db, user_id, ex_id, 7, [(22.0, 12)])
    await _log_session(db, user_id, ex_id, 1, [(24.0, 12)])

    last_session, step = await workout._exercise_history(ex_id)

    assert last_session == [(24.0, 12, None)]
    assert step == pytest.approx(2.0)
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 26×9" in hint  # a dumbbell that exists, not 26.5


@pytest.mark.asyncio
async def test_backoff_sets_do_not_become_the_step(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Жим лёжа")
    await _log_session(db, user_id, ex_id, 1, [(100.0, 12), (80.0, 12)])

    last_session, step = await workout._exercise_history(ex_id)

    assert step == pytest.approx(20.0)  # inferred, but too coarse to be believed
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 102.5×11" in hint


@pytest.mark.asyncio
async def test_first_ever_session_falls_back_to_the_default_step(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Тяга блока")
    await _log_session(db, user_id, ex_id, 1, [(50.0, 12)])

    last_session, step = await workout._exercise_history(ex_id)

    assert step is None
    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 52.5×10" in hint


@pytest.mark.asyncio
async def test_heavy_lift_without_history_jumps_by_five(fresh_db, user_id):
    db = fresh_db
    ex_id = await _exercise(db, user_id, "Становая")
    await _log_session(db, user_id, ex_id, 1, [(210.0, 12)])

    last_session, step = await workout._exercise_history(ex_id)

    hint = workout._logging_hint(
        last_session, has_sets=True, unit="kg", show_progression=True, inferred_step=step
    )
    assert "🎯 Цель: 215×11" in hint


def test_the_programs_rep_ceiling_beats_the_global_default():
    """«Доходишь до 8 повторов — прибавляй» раньше было текстом в превью: цель
    считалась по глобальному REP_RANGE_MAX (10) и на 8 повторах молчала."""
    rule = {"rule": "double_progression", "reps_top": 8, "step": 2.5}

    without = analytics.suggest_progression([(80, 8)])
    with_rule = analytics.suggest_progression([(80, 8)], rule=rule)

    assert without.action == "add_reps" and without.target_reps == 9
    assert with_rule.action == "add_weight"
    assert with_rule.target_weight == 82.5
    assert with_rule.from_rule is True


def test_the_programs_step_beats_the_inferred_one():
    rule = {"rule": "double_progression", "reps_top": 8, "step": 5}
    suggestion = analytics.suggest_progression([(80, 10)], inferred_step=2.5, rule=rule)
    assert suggestion.target_weight == 85


def test_linear_load_adds_weight_every_session_whatever_the_reps():
    rule = {"rule": "linear_load", "step": 2.5}
    suggestion = analytics.suggest_progression([(60, 5)], rule=rule)
    assert (suggestion.action, suggestion.target_weight, suggestion.target_reps) == (
        "add_weight", 62.5, 5,
    )
    assert suggestion.from_rule is True


def test_a_malformed_rule_falls_back_to_the_default():
    """Правило приходит от модели — мусор в нём не должен ломать подсказку."""
    for rule in ({"rule": "нечто"}, {"rule": "linear_load"}, {"rule": "linear_load", "step": "много"},
                 {"rule": "double_progression", "reps_top": 0}, {}):
        suggestion = analytics.suggest_progression([(80, 8)], rule=rule)
        assert suggestion == analytics.suggest_progression([(80, 8)]), rule


def test_the_hint_says_when_the_number_came_from_the_program():
    rule = {"rule": "double_progression", "reps_top": 8, "step": 2.5}
    text = formatting.format_progression_hint(analytics.suggest_progression([(80, 8)], rule=rule))
    assert "по программе" in text
    plain = formatting.format_progression_hint(analytics.suggest_progression([(80, 10)]))
    assert "по программе" not in plain


# ---------- шаг правила при смене единиц ----------


async def _routine_with_rule(db, user_id, rule_json='{"rule": "linear_load", "step": 2.5}'):
    ex_id = await _exercise(db, user_id, "Присед")
    rid = await db.create_routine(user_id, "Ноги")
    await db.add_routine_exercise(rid, ex_id, 0, "5×5")
    entry = (await db.list_routine_exercises(rid))[0]
    await db.set_routine_exercise_progression(entry["id"], rule_json)
    return rid, entry["id"], ex_id


async def _stored_rule(db, re_id):
    row = await db.get_routine_exercise(re_id)
    return json.loads(row["progression"]) if row["progression"] else None


@pytest.mark.asyncio
async def test_switching_units_rescales_the_programs_step_too(fresh_db, user_id):
    """step хранится в единицах пользователя: сохранённые «+2.5 кг» после
    перехода на фунты без пересчёта читались бы как «+2.5 lb»."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    import config
    from handlers import settings

    db = fresh_db
    _rid, re_id, _ex_id = await _routine_with_rule(db, user_id)

    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = "settings:unityes"
    callback.answer = AsyncMock()
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )

    await settings.settings_unit(callback, state)

    assert (await db.get_user(user_id))["unit"] == "lb"
    rule = await _stored_rule(db, re_id)
    assert rule["step"] == pytest.approx(2.5 * config.LB_PER_KG, abs=0.01)  # ~5.51


@pytest.mark.asyncio
async def test_step_rescale_skips_garbage_and_other_users(fresh_db, user_id):
    db = fresh_db
    _rid, good_id, _ex = await _routine_with_rule(db, user_id)
    _rid2, broken_id, _ex2 = await _routine_with_rule(db, user_id, "{не json")
    await db.get_or_create_user(telegram_id=222, username="other")
    _rid3, foreign_id, _ex3 = await _routine_with_rule(db, 222)

    # Битый JSON не роняет пересчёт и остаётся как был (его игнорирует и подсказка).
    await db.scale_progression_steps(user_id, 2.0)

    assert (await _stored_rule(db, good_id))["step"] == pytest.approx(5.0)
    assert (await db.get_routine_exercise(broken_id))["progression"] == "{не json"
    assert (await _stored_rule(db, foreign_id))["step"] == pytest.approx(2.5)


# ---------- сохранённое правило → живая тренировка ----------


@pytest.mark.asyncio
async def test_the_rule_reaches_a_workout_started_from_the_day(fresh_db, user_id):
    db = fresh_db
    rid, _re_id, ex_id = await _routine_with_rule(
        db, user_id, '{"rule": "double_progression", "reps_top": 8, "step": 2.5}'
    )
    wid = await db.create_workout(user_id, routine_id=rid)

    rule = await db.progression_rule_for_workout(wid, ex_id)

    assert rule == {"rule": "double_progression", "reps_top": 8, "step": 2.5}


@pytest.mark.asyncio
async def test_a_workout_off_program_has_no_rule(fresh_db, user_id):
    db = fresh_db
    _rid, _re_id, ex_id = await _routine_with_rule(db, user_id)
    wid = await db.create_workout(user_id)  # «с нуля», без routine_id
    assert await db.progression_rule_for_workout(wid, ex_id) is None


@pytest.mark.asyncio
async def test_a_broken_stored_rule_degrades_to_none(fresh_db, user_id):
    """Правило пишет модель — мусор в колонке не должен ломать экран записи
    подхода, подсказка просто считает по умолчанию."""
    db = fresh_db
    rid, _re_id, ex_id = await _routine_with_rule(db, user_id, "{не json")
    wid = await db.create_workout(user_id, routine_id=rid)
    assert await db.progression_rule_for_workout(wid, ex_id) is None
