"""Тренер как руки, а не только как голова.

Граница здесь одна и она проходит по обратимости: переименовать, скопировать,
перенести в другую группу, записать вес — инструмент делает сам, потому что
всё это откатывается. Удалить, объединить, заархивировать, поделиться —
только предлагает кнопкой: разобрать слитые программы обратно UI не умеет, а
визитку, ушедшую в чат, не вернуть.
"""

import datetime as dt

import pytest

import ai_trainer
import db as dbmod

pytestmark = pytest.mark.asyncio


async def _program(db, user_id: int, name: str, days: list[str]) -> int:
    program_id = await db.create_program(user_id, name)
    for day in days:
        await db.create_routine(user_id, day, program_id=program_id)
    return program_id


# ---------- программы ----------


async def test_rename_program_happens_immediately(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, "Программа от 02.08", ["Ноги"])

    payload = await ai_trainer._rename_program(
        user_id, {"name": "Программа от 02.08", "new_name": "PPL"}
    )

    assert payload["ok"] is True
    assert (await dbmod.get_program(program_id))["name"] == "PPL"


async def test_rename_program_refuses_a_taken_name(fresh_db, user_id):
    """Молчаливое слияние по имени однажды уже съедало программу — теперь отказ."""
    db = fresh_db
    await _program(db, user_id, "Альфа", ["Ноги"])
    beta_id = await _program(db, user_id, "Бета", ["Верх"])

    payload = await ai_trainer._rename_program(user_id, {"name": "Бета", "new_name": "Альфа"})

    assert "занято" in payload["error"]
    assert (await dbmod.get_program(beta_id))["name"] == "Бета"


async def test_copy_program_duplicates_days_and_targets(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, "PPL", ["Жим", "Тяга"])
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group)
    day = (await db.list_program_days_by_id(program_id))[0]
    await db.append_routine_exercise(day["id"], bench, "3x8")

    payload = await ai_trainer._copy_program(user_id, {"name": "PPL"})

    assert payload["copied"]["days"] == 2
    copy = await dbmod.find_program_by_name(user_id, payload["copied"]["to"])
    copy_days = await dbmod.list_program_days_by_id(copy["id"])
    assert [d["name"] for d in copy_days] == ["Жим", "Тяга"]
    assert [(e["exercise_id"], e["target"]) for e in await dbmod.list_routine_exercises(copy_days[0]["id"])] == [
        (bench, "3x8")
    ]
    # Оригинал на месте — копия делается, чтобы его не трогать.
    assert (await dbmod.get_program(program_id))["name"] == "PPL"


async def test_copy_program_takes_a_free_name_next_to_the_original(fresh_db, user_id):
    db = fresh_db
    await _program(db, user_id, "PPL", ["Жим"])
    payload = await ai_trainer._copy_program(user_id, {"name": "PPL"})
    assert payload["copied"]["to"] != "PPL"


async def test_merge_only_proposes_and_never_merges_itself(fresh_db, user_id):
    db = fresh_db
    source = await _program(db, user_id, "Вика", ["Ноги"])
    target = await _program(db, user_id, "Вика (2)", ["Верх"])

    payload, action = await ai_trainer._merge_programs(user_id, {"name": "Вика", "into": "Вика (2)"})

    assert action == {
        "label": "🔗 Объединить: Вика → Вика (2)",
        "callback": f"ai:pgmmergeask:{source}:{target}",
    }
    assert "НЕ ОБЪЕДИНЕНО" in payload["note"]
    assert await dbmod.get_program(source) is not None


async def test_merge_refuses_a_solo_program(fresh_db, user_id):
    """Одиночная программа — это один день; вынуть его обратно UI не умеет."""
    db = fresh_db
    await _program(db, user_id, "Вика", ["Ноги"])
    await db.create_routine(user_id, "Домашка")

    payload, action = await ai_trainer._merge_programs(user_id, {"name": "Домашка", "into": "Вика"})

    assert action is None
    assert "многодневки" in payload["error"]


async def test_share_program_points_at_the_card_button(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, "PPL", ["Жим"])
    solo_id = await db.create_routine(user_id, "Домашка")

    _, program_action = await ai_trainer._share_program(user_id, {"name": "PPL"})
    _, solo_action = await ai_trainer._share_program(user_id, {"name": "Домашка"})

    assert program_action["callback"] == f"share:prg:{program_id}"
    assert solo_action["callback"] == f"share:rt:{solo_id}"


# ---------- упражнения ----------


async def test_create_exercise_needs_a_real_group(fresh_db, user_id):
    payload = await ai_trainer._create_exercise(user_id, {"name": "Болгарские выпады", "group": "Крылья"})
    assert "Ноги" in payload["muscle_groups"]
    assert await dbmod.find_exercise_by_name(user_id, "Болгарские выпады") is None


async def test_create_exercise_reuses_what_is_already_there(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    await db.create_exercise(user_id, "Присед", group)

    payload = await ai_trainer._create_exercise(user_id, {"name": "присед", "group": "Ноги"})

    assert payload["already_exists"] == "Присед"
    assert await dbmod.count_user_exercises(user_id) == 1


async def test_rename_exercise_keeps_its_history(fresh_db, user_id):
    """Переименование идёт в той же строке — иначе рекорды остались бы у старой."""
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим", group)

    payload = await ai_trainer._rename_exercise(user_id, {"name": "Жим", "new_name": "Жим лёжа"})

    assert payload["renamed"] == {"from": "Жим", "to": "Жим лёжа"}
    assert (await dbmod.get_exercise(bench))["display_name"] == "Жим лёжа"


async def test_move_exercise_changes_only_the_group(fresh_db, user_id):
    db = fresh_db
    groups = {g["name"]: g["id"] for g in await db.list_muscle_groups(user_id)}
    lunges = await db.create_exercise(user_id, "Выпады", groups["Другое"])

    await ai_trainer._move_exercise(user_id, {"name": "Выпады", "group": "Ноги"})

    assert (await dbmod.get_exercise(lunges))["primary_group_id"] == groups["Ноги"]


async def test_archive_exercise_only_proposes(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Другое")
    fly = await db.create_exercise(user_id, "Сведения", group)

    payload, action = await ai_trainer._archive_exercise(user_id, {"name": "Сведения"})

    assert action == {"label": "🗄 В архив: Сведения", "callback": f"ai:exarchask:{fly}"}
    assert "НЕ АРХИВИРОВАНО" in payload["note"]
    assert (await dbmod.get_exercise(fly))["is_archived"] == 0


async def test_exercise_tools_do_not_touch_catalog_templates(fresh_db, user_id):
    """«Переименуй жим» про шаблон каталога переименовало бы его всем."""
    templates = await dbmod.list_all_exercise_templates()
    payload = await ai_trainer._rename_exercise(
        user_id, {"name": templates[0]["display_name"], "new_name": "Моё"}
    )
    assert "нет" in payload["error"]


# ---------- дневники ----------


async def test_log_bodyweight_writes_it_straight_away(fresh_db, user_id):
    payload, action = await ai_trainer._log_bodyweight(user_id, {"weight": 78.4})

    assert payload["ok"] is True
    assert (await dbmod.get_latest_bodyweight(user_id))["weight"] == 78.4
    # Запись уже сделана — кнопка ведёт прямо в дневник веса, не спрашивая
    # подтверждения (в отличие от действий вроде удаления программы).
    assert action == {"label": "⚖️ Дневник веса", "callback": "menu:bodyweight"}


async def test_log_bodyweight_rejects_nonsense(fresh_db, user_id):
    payload, action = await ai_trainer._log_bodyweight(user_id, {"weight": 4})
    assert "error" in payload
    assert action is None
    assert await dbmod.get_latest_bodyweight(user_id) is None


async def test_log_food_lands_in_todays_diary(fresh_db, user_id):
    payload = await ai_trainer._log_food(
        user_id, {"description": "Овсянка с бананом", "calories": 420, "protein": 12}
    )

    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert [e["description"] for e in entries] == ["Овсянка с бананом"]
    assert entries[0]["calories"] == 420


async def test_log_food_without_numbers_is_still_an_entry(fresh_db, user_id):
    """Выдуманные калории хуже, чем их отсутствие."""
    payload = await ai_trainer._log_food(user_id, {"description": "Два яйца"})
    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert entries[0]["calories"] is None


# ---------- сравнение периодов ----------


async def _log(db, user_id: int, ex_id: int, day: dt.date, weight: float, reps: int, sets: int = 3):
    workout_id = await db.create_workout(user_id, started_at=f"{day.isoformat()}T10:00:00")
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=f"{day.isoformat()}T11:00:00")


async def test_compare_periods_puts_the_biggest_shift_first(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", group)
    curl = await db.create_exercise(user_id, "Сгибания", group)
    today = dt.date.today()

    await _log(db, user_id, squat, today - dt.timedelta(days=100), 100, 5)
    await _log(db, user_id, squat, today - dt.timedelta(days=10), 130, 5)
    await _log(db, user_id, curl, today - dt.timedelta(days=100), 40, 10)
    await _log(db, user_id, curl, today - dt.timedelta(days=10), 42.5, 10)

    payload = await ai_trainer._compare_periods(user_id, {"days": 90})

    assert [r["exercise"] for r in payload["exercises"]] == ["Присед", "Сгибания"]
    assert payload["exercises"][0]["e1rm_delta"] > payload["exercises"][1]["e1rm_delta"]
    assert payload["exercises"][0]["before"]["workouts"] == 1


async def test_compare_periods_names_what_started_and_stopped(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    dropped = await db.create_exercise(user_id, "Разгибания", group)
    started = await db.create_exercise(user_id, "Присед", group)
    today = dt.date.today()

    await _log(db, user_id, dropped, today - dt.timedelta(days=100), 50, 12)
    await _log(db, user_id, started, today - dt.timedelta(days=5), 100, 5)

    payload = await ai_trainer._compare_periods(user_id, {"days": 90})

    assert payload["stopped_doing"] == ["Разгибания"]
    assert payload["started_doing"] == ["Присед"]


async def test_compare_periods_ignores_exercises_with_no_sets_in_either_window(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    await db.create_exercise(user_id, "Никогда не делал", group)

    payload = await ai_trainer._compare_periods(user_id, {"days": 30})

    assert payload["exercises"] == []
