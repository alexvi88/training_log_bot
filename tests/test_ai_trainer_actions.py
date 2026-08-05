"""Тренер как руки, а не только как голова.

Граница здесь одна и она проходит по обратимости: переименовать, скопировать,
перенести в другую группу, записать вес — инструмент делает сам, потому что
всё это откатывается. Удалить, объединить, заархивировать, поделиться —
только предлагает кнопкой: разобрать слитые программы обратно UI не умеет, а
визитку, ушедшую в чат, не вернуть.

Но обратимость и возможность откатить — разные вещи, и вторую половину файла
занимает именно она: всё, что тренер делает сам, обязано вернуть описание
отката, а откат — вернуть как было.
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

    payload, _undo = await ai_trainer._rename_program(
        user_id, {"name": "Программа от 02.08", "new_name": "PPL"}
    )

    assert payload["ok"] is True
    assert (await dbmod.get_program(program_id))["name"] == "PPL"


async def test_rename_program_refuses_a_taken_name(fresh_db, user_id):
    """Молчаливое слияние по имени однажды уже съедало программу — теперь отказ."""
    db = fresh_db
    await _program(db, user_id, "Альфа", ["Ноги"])
    beta_id = await _program(db, user_id, "Бета", ["Верх"])

    payload, _undo = await ai_trainer._rename_program(user_id, {"name": "Бета", "new_name": "Альфа"})

    assert "занято" in payload["error"]
    assert (await dbmod.get_program(beta_id))["name"] == "Бета"


async def test_copy_program_duplicates_days_and_targets(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, "PPL", ["Жим", "Тяга"])
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group)
    day = (await db.list_program_days_by_id(program_id))[0]
    await db.append_routine_exercise(day["id"], bench, "3x8")

    payload, _undo = await ai_trainer._copy_program(user_id, {"name": "PPL"})

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
    payload, _undo = await ai_trainer._copy_program(user_id, {"name": "PPL"})
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
    payload, _undo = await ai_trainer._create_exercise(user_id, {"name": "Болгарские выпады", "group": "Крылья"})
    assert "Ноги" in payload["muscle_groups"]
    assert await dbmod.find_exercise_by_name(user_id, "Болгарские выпады") is None


async def test_create_exercise_reuses_what_is_already_there(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Ноги")
    await db.create_exercise(user_id, "Присед", group)

    payload, _undo = await ai_trainer._create_exercise(user_id, {"name": "присед", "group": "Ноги"})

    assert payload["already_exists"] == "Присед"
    assert await dbmod.count_user_exercises(user_id) == 1


async def test_rename_exercise_keeps_its_history(fresh_db, user_id):
    """Переименование идёт в той же строке — иначе рекорды остались бы у старой."""
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим", group)

    payload, _undo = await ai_trainer._rename_exercise(user_id, {"name": "Жим", "new_name": "Жим лёжа"})

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
    payload, _undo = await ai_trainer._rename_exercise(
        user_id, {"name": templates[0]["display_name"], "new_name": "Моё"}
    )
    assert "нет" in payload["error"]


# ---------- дневники ----------


async def test_log_bodyweight_writes_it_straight_away(fresh_db, user_id):
    payload, _undo = await ai_trainer._log_bodyweight(user_id, {"weight": 78.4})

    assert payload["ok"] is True
    assert (await dbmod.get_latest_bodyweight(user_id))["weight"] == 78.4


async def test_log_bodyweight_rejects_nonsense(fresh_db, user_id):
    payload, _undo = await ai_trainer._log_bodyweight(user_id, {"weight": 4})
    assert "error" in payload
    assert await dbmod.get_latest_bodyweight(user_id) is None


async def test_log_food_lands_in_todays_diary(fresh_db, user_id):
    payload, _undo = await ai_trainer._log_food(
        user_id, {"description": "Овсянка с бананом", "calories": 420, "protein": 12}
    )

    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert [e["description"] for e in entries] == ["Овсянка с бананом"]
    assert entries[0]["calories"] == 420


async def test_log_food_without_numbers_is_still_an_entry(fresh_db, user_id):
    """Выдуманные калории хуже, чем их отсутствие."""
    payload, _undo = await ai_trainer._log_food(user_id, {"description": "Два яйца"})
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


# ---------- откат того, что тренер сделал сам ----------

import json  # noqa: E402

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

import handlers.ai_trainer as ai_handler  # noqa: E402


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def _call(user_id: int, tool: str, payload: dict) -> dict | None:
    """Вызвать инструмент через execute_tool и забрать описание отката."""
    captured: list[dict] = []

    async def on_action(action: dict) -> None:
        captured.append(action)

    await ai_trainer.execute_tool(user_id, tool, payload, on_action=on_action)
    return captured[-1] if captured else None


async def test_every_self_acting_tool_offers_an_undo(fresh_db, user_id):
    """Опись, а не проверка одного случая: новый инструмент, который делает
    что-то сам, не должен незаметно проехать мимо кнопки отката."""
    assert set(ai_trainer._UNDOABLE_TOOLS) == {
        "log_bodyweight", "log_food", "create_exercise", "rename_exercise",
        "move_exercise_to_group", "rename_program", "copy_program",
        "save_athlete_profile",
    }
    # И ни один из них не должен заодно оказаться среди тех, что только предлагают.
    assert not set(ai_trainer._UNDOABLE_TOOLS) & set(ai_trainer._ACTION_TOOLS)


async def test_logged_bodyweight_can_be_taken_back(fresh_db, user_id):
    action = await _call(user_id, "log_bodyweight", {"weight": 78.4})

    assert action is not None and action["undo"]["kind"] == "bodyweight"
    assert len(await fresh_db.list_bodyweight_logs(user_id)) == 1

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    assert await fresh_db.list_bodyweight_logs(user_id) == []


async def test_undo_removes_that_entry_not_the_latest(fresh_db, user_id):
    """Между записью тренера и тапом человек мог взвеситься ещё раз руками —
    снос «последней» утащил бы не то."""
    action = await _call(user_id, "log_bodyweight", {"weight": 78.4})
    await fresh_db.add_bodyweight_log(user_id, 81.0)

    await ai_handler._apply_undo(user_id, action["undo"])

    left = [row["weight"] for row in await fresh_db.list_bodyweight_logs(user_id)]
    assert left == [81.0]


async def test_logged_food_can_be_taken_back(fresh_db, user_id):
    action = await _call(user_id, "log_food", {"description": "Овсянка", "calories": 300})
    assert action["undo"]["kind"] == "food"

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    today = dt.date.today().isoformat()
    assert await fresh_db.list_food_entries(user_id, today) == []


async def test_created_exercise_is_removed_whole_not_archived(fresh_db, user_id):
    await fresh_db.create_muscle_group(user_id, "Ноги")
    action = await _call(user_id, "create_exercise", {"name": "Зашагивания", "group": "Ноги"})

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    # Именно снесено: в архиве копился бы мусор, которого человек не заводил.
    assert await fresh_db.get_exercise(action["undo"]["id"]) is None


async def test_undo_refuses_to_delete_an_exercise_that_already_has_sets(fresh_db, user_id):
    """Между созданием и откатом по упражнению успели записать подход — снос
    утащил бы за собой чужие данные."""
    await fresh_db.create_muscle_group(user_id, "Ноги")
    action = await _call(user_id, "create_exercise", {"name": "Зашагивания", "group": "Ноги"})
    ex_id = action["undo"]["id"]
    await _log(fresh_db, user_id, ex_id, dt.date(2026, 8, 1), weight=40, reps=8, sets=1)

    assert await ai_handler._apply_undo(user_id, action["undo"]) is None
    assert await fresh_db.get_exercise(ex_id) is not None


async def test_renames_carry_the_old_name_into_the_undo(fresh_db, user_id):
    """Раньше откат существовал, только если старое имя помнил сам человек."""
    await _program(fresh_db, user_id, "Альфа", ["День 1"])
    action = await _call(user_id, "rename_program", {"name": "Альфа", "new_name": "Бета"})

    assert "Альфа" in action["label"]
    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    assert (await fresh_db.find_program_by_name(user_id, "Альфа")) is not None


async def test_copied_program_is_removed_by_its_undo(fresh_db, user_id):
    await _program(fresh_db, user_id, "PPL", ["Push", "Pull"])
    action = await _call(user_id, "copy_program", {"name": "PPL"})
    copy_id = action["undo"]["id"]

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    assert await fresh_db.get_program(copy_id) is None
    # Оригинал не тронут.
    assert await fresh_db.find_program_by_name(user_id, "PPL") is not None


async def test_moved_exercise_goes_back_to_its_old_group(fresh_db, user_id):
    legs = await fresh_db.create_muscle_group(user_id, "Ноги")
    await fresh_db.create_muscle_group(user_id, "Спина")
    ex_id = await fresh_db.create_exercise(user_id, "Выпады", legs)
    action = await _call(user_id, "move_exercise_to_group", {"name": "Выпады", "group": "Спина"})

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    assert (await fresh_db.get_exercise(ex_id))["primary_group_id"] == legs


async def test_profile_undo_restores_only_the_fields_that_call_changed(fresh_db, user_id):
    """Профиль тренер пишет не дожидаясь просьбы, и до этого его нельзя было ни
    увидеть, ни откатить. Восстанавливать целиком нельзя: затёрло бы и то, что
    человек успел поправить между записью и тапом."""
    await ai_trainer.execute_tool(user_id, "save_athlete_profile", {"goal": "масса"})
    action = await _call(user_id, "save_athlete_profile", {"limitations": "болит плечо"})

    assert action["undo"]["before"] == {"limitations": None}
    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None

    user = await fresh_db.get_user(user_id)
    assert user["limitations"] is None
    assert user["goal"] == "масса"  # чужое поле не тронуто


async def test_undo_keys_survive_the_json_fsm_round_trip(fresh_db, user_id):
    """Ключи намеренно не числовые: состояние FSM лежит в JSON, а
    fsm_storage._restore_int_keys превращает «7» обратно в int 7 — после
    перезапуска поиск по строке из callback_data не нашёл бы ничего."""
    import fsm_storage

    state = _state(user_id)
    buttons = await ai_handler._register_undos(
        state, [{"label": "↩️ Отменить", "undo": {"kind": "bodyweight", "id": 1}}]
    )
    key = buttons[0]["callback"].split(":", 2)[2]

    store = (await state.get_data())["ai_undo"]
    restored = fsm_storage._restore_int_keys(json.loads(json.dumps(store)))

    assert key in restored


async def test_actions_awaiting_confirmation_pass_through_untouched(fresh_db, user_id):
    """У delete_program кнопка не откатывает, а делает — её трогать нельзя."""
    state = _state(user_id)
    original = {"label": "🗑 Удалить: X", "callback": "rt:pgmdelask:5"}

    out = await ai_handler._register_undos(state, [dict(original)])

    assert out == [original]
    assert not (await state.get_data()).get("ai_undo")


async def test_only_the_last_few_undos_are_kept(fresh_db, user_id):
    """Состояние FSM уезжает на диск целиком при каждой записи — копить
    описания откатов вечно незачем."""
    state = _state(user_id)
    for i in range(ai_handler._UNDO_SLOTS + 4):
        await ai_handler._register_undos(
            state, [{"label": "↩️", "undo": {"kind": "bodyweight", "id": i}}]
        )

    store = (await state.get_data())["ai_undo"]
    assert len(store) == ai_handler._UNDO_SLOTS
    # Выживают свежие, а не первые попавшиеся.
    assert "u1" not in store
    assert f"u{ai_handler._UNDO_SLOTS + 4}" in store
