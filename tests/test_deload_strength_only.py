"""Плановая разгрузка — только в силовых программах.

Решение продукта: «🪫 Разгрузка каждые N недель» в программе на массу, общую
форму, похудение или для новичка — потерянная неделя, а не восстановление.
Решает сервер по цели программы (propose_program.goal), а не модель по памяти
о промпте: на массе её разгрузка отбрасывается, и модели говорится почему.
"""

import ai_program_actions
import ai_trainer
import i18n
import progression_data
import seed_data

DAY = [{"name": "День 1", "exercises": [
    {"name": "Присед со штангой", "sets": 4, "reps_min": 3, "reps_max": 5},
]}]


async def _propose(user_id, **fields):
    drafts = []

    async def _keep(draft):
        drafts.append(draft)

    raw = await ai_trainer.execute_tool(
        user_id, "propose_program",
        {"name": "Программа", "description": "Пара фраз.", "days": DAY, **fields},
        on_program=_keep,
    )
    return raw, drafts[-1]


def test_goal_is_required_and_closed_in_the_tool_schema():
    tool = next(t for t in ai_trainer.TOOLS if t["function"]["name"] == "propose_program")
    params = tool["function"]["parameters"]
    assert "goal" in params["required"]
    assert params["properties"]["goal"]["enum"] == list(ai_trainer.PROGRAM_GOALS)
    assert "deload_every_weeks" not in params["required"]


def test_deload_for_goal():
    assert ai_trainer.deload_for_goal("strength", 5) == 5
    for goal in ("hypertrophy", "general", "fat_loss", None):
        assert ai_trainer.deload_for_goal(goal, 5) is None
    assert ai_trainer.clean_program_goal(" Strength ") == "strength"
    assert ai_trainer.clean_program_goal("powerlifting") is None
    assert ai_trainer.clean_program_goal(None) is None


async def test_strength_program_keeps_its_deload(fresh_db, user_id):
    raw, draft = await _propose(user_id, goal="strength", deload_every_weeks=5)
    assert draft["deload_every_weeks"] == 5
    assert "deload_dropped" not in raw
    saved = await ai_program_actions.finalize_program_save(user_id, draft)
    program = await fresh_db.get_program(saved["program_id"])
    assert program["deload_every_weeks"] == 5


async def test_hypertrophy_program_drops_the_deload_the_model_put_in(fresh_db, user_id):
    raw, draft = await _propose(user_id, goal="hypertrophy", deload_every_weeks=5)
    assert draft["deload_every_weeks"] is None
    # Модель узнаёт, что разгрузки нет, — иначе пообещает её в ответе.
    assert "deload_dropped" in raw
    saved = await ai_program_actions.finalize_program_save(user_id, draft)
    program = await fresh_db.get_program(saved["program_id"])
    assert program["deload_every_weeks"] is None


async def test_missing_goal_means_no_deload(fresh_db, user_id):
    raw, draft = await _propose(user_id, deload_every_weeks=4)
    assert draft["deload_every_weeks"] is None
    assert "deload_dropped" in raw


async def test_general_program_without_deload_says_nothing_about_it(fresh_db, user_id):
    raw, draft = await _propose(user_id, goal="general")
    assert draft["deload_every_weeks"] is None
    assert "deload_dropped" not in raw


async def test_catalog_copies_carry_no_deload(fresh_db, user_id):
    # В каталоге разгрузки нет ни у одной программы, включая силовую 5×5:
    # копия из каталога не должна приносить её с собой.
    with i18n.use_lang("ru"):
        for program in seed_data.WORKOUT_PROGRAMS:
            program_id = await seed_data.instantiate_program(
                user_id, program["key"], f"Копия {program['key']}"
            )
            assert (await fresh_db.get_program(program_id))["deload_every_weeks"] is None


async def test_live_workout_without_deload_has_no_deload_week(fresh_db, user_id):
    # Программа без разгрузки на той неделе, где у «каждой 4-й» была бы
    # разгрузка: тренировка обычная, подсказка без срезанных подходов.
    program_id = await fresh_db.create_program(user_id, "Масса")
    await fresh_db.conn().execute(
        "UPDATE programs SET created_at = ? WHERE id = ?", ("2026-03-02T10:00:00", program_id)
    )
    await fresh_db.conn().commit()
    routine_id = await fresh_db.create_routine(user_id, "День 1", program_id=program_id)
    workout_id = await fresh_db.create_workout(
        user_id, started_at="2026-03-23T10:00:00", routine_id=routine_id
    )
    assert await fresh_db.deload_week_for_workout(workout_id) is False

    with i18n.use_lang("en"):
        hint = progression_data.hint(
            [(100, 12, None)], [], unit="kg", formula="epley", is_deload=False
        )
    assert hint["is_deload"] is False
    assert "Deload" not in hint["text"]
