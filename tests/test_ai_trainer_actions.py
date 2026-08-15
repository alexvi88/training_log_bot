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

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

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


async def test_archive_exercises_proposes_one_action_for_all(fresh_db, user_id):
    """Живой репорт: «заархивируй все неиспользуемые» на 23 упражнения
    поставило под ответом 23 кнопки «В архив: X» подряд, вызвав
    archive_exercise в цикле — должна быть одна кнопка на все сразу."""
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Другое")
    fly = await db.create_exercise(user_id, "Сведения", group)
    curl = await db.create_exercise(user_id, "Подъём на бицепс", group)

    payload, action = await ai_trainer._archive_exercises(
        user_id, {"names": ["Сведения", "Подъём на бицепс"]}
    )

    assert action["label"] == "🗄 В архив всё (2)"
    assert set(action["archive_ids"]) == {fly, curl}
    assert "НЕ АРХИВИРОВАНО" in payload["note"]
    assert "Сведения" in payload["note"] and "Подъём на бицепс" in payload["note"]
    assert (await dbmod.get_exercise(fly))["is_archived"] == 0
    assert (await dbmod.get_exercise(curl))["is_archived"] == 0


async def test_archive_exercises_names_the_ones_it_could_not_find(fresh_db, user_id):
    db = fresh_db
    group = await db.create_muscle_group(user_id, "Другое")
    fly = await db.create_exercise(user_id, "Сведения", group)

    payload, action = await ai_trainer._archive_exercises(
        user_id, {"names": ["Сведения", "Придуманное упражнение"]}
    )

    assert action["archive_ids"] == [fly]
    assert "Придуманное упражнение" in payload["note"]


async def test_archive_exercises_errors_when_none_resolve(fresh_db, user_id):
    payload, action = await ai_trainer._archive_exercises(user_id, {"names": ["Несуществующее"]})

    assert action is None
    assert "error" in payload


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
    """Без оценщика (ключа нет) запись всё равно ложится — просто без цифр."""
    payload, _undo = await ai_trainer._log_food(user_id, {"description": "Два яйца"})
    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert entries[0]["calories"] is None
    assert payload["macros_estimated"] is False


async def test_log_food_without_numbers_asks_the_same_estimator_as_the_diary(
    fresh_db, user_id, monkeypatch
):
    """Регрессия: одно и то же «хачапури» давало 865 ккал с экрана дневника и
    пустую запись из чата с тренером, молча занижая итог дня."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(uid, text="", **kwargs):
        assert text == "Хачапури"
        return {"is_food": True, "calories": 865, "protein": 35, "fat": 45, "carbs": 80}

    monkeypatch.setattr(ai_trainer, "analyze_food", fake_analyze)

    payload, _undo = await ai_trainer._log_food(user_id, {"description": "Хачапури"})

    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert entries[0]["calories"] == 865
    # Человек этих цифр не называл — тренер обязан сказать, что они прикидочные.
    assert payload["macros_estimated"] is True


async def test_log_food_estimate_respects_ai_limits(fresh_db, user_id, monkeypatch):
    """Регрессия: чат с тренером (и MCP тем же путём) звал платный analyze_food
    в обход ai_limits — раньше ни дневная квота еды, ни денежный стоп-кран сюда
    не заглядывали вовсе, только экран 🍽 Дневник еды."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fail(*a, **kw):
        raise AssertionError("оценщик не должен вызываться при заблокированной квоте")

    monkeypatch.setattr(ai_trainer, "analyze_food", fail)
    monkeypatch.setattr(
        ai_trainer.ai_limits, "check",
        AsyncMock(return_value=ai_trainer.ai_limits.Block(kind="food", log="food: 30 из 30 за сутки")),
    )
    before = await dbmod.get_ai_food_count_today(user_id)

    payload, _undo = await ai_trainer._log_food(user_id, {"description": "Хачапури"})

    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert entries[0]["calories"] is None
    assert payload["macros_estimated"] is False
    assert await dbmod.get_ai_food_count_today(user_id) == before


async def test_log_food_keeps_numbers_the_person_named(fresh_db, user_id, monkeypatch):
    """Названные вслух цифры важнее оценки: оценщик к ним не зовётся вовсе."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fail(*a, **kw):
        raise AssertionError("оценщик не должен вызываться, цифры уже есть")

    monkeypatch.setattr(ai_trainer, "analyze_food", fail)

    payload, _undo = await ai_trainer._log_food(
        user_id, {"description": "Овсянка", "calories": 300}
    )
    assert payload["logged"]["calories"] == 300
    assert payload["macros_estimated"] is False


async def test_log_food_does_not_invent_numbers_for_a_non_meal(fresh_db, user_id, monkeypatch):
    """«Поел» — не еда: оценщик отказывается, и запись остаётся без цифр."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def not_food(uid, text="", **kwargs):
        return {"is_food": False, "calories": None}

    monkeypatch.setattr(ai_trainer, "analyze_food", not_food)

    payload, _undo = await ai_trainer._log_food(user_id, {"description": "Поел"})

    entries = await dbmod.list_food_entries(user_id, payload["logged"]["date"])
    assert entries[0]["calories"] is None
    assert payload["macros_estimated"] is False


async def test_estimate_missing_macros_lets_a_timed_out_call_finish_in_background(
    fresh_db, user_id, monkeypatch
):
    """asyncio.wait_for cancels what it wraps on timeout — if analyze_food had
    already reached the provider by then, that used to cancel the request
    client-side before its own cost-logging line ever ran, losing a call we
    were billed for. asyncio.shield keeps the underlying call alive past the
    timeout: we stop waiting for its answer, but it still runs to completion
    (and logs its own cost) in the background."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_FOOD_ESTIMATE_TIMEOUT", 0.01)

    finished = asyncio.Event()

    async def slow_analyze(uid, text="", **kwargs):
        await asyncio.sleep(0.05)  # дольше таймаута — успевает состояться
        finished.set()
        return {"is_food": True, "calories": 300, "protein": 10, "fat": 5, "carbs": 40}

    monkeypatch.setattr(ai_trainer, "analyze_food", slow_analyze)

    entry: dict = {}
    got = await ai_trainer._estimate_missing_macros(user_id, "что-то съел", entry)

    assert got is False  # тайм-аут снаружи всё равно честно отдаёт "не успел"
    assert entry == {}  # запись не подождала цифр — они не пишутся в неё

    await asyncio.wait_for(finished.wait(), timeout=1)  # но вызов реально состоялся


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

import config  # noqa: E402
import handlers.ai_trainer as ai_handler  # noqa: E402


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def _call(user_id: int, tool: str, payload: dict) -> dict | None:
    """Вызвать инструмент через execute_tool и забрать описание отката.

    log_bodyweight отдаёт две кнопки (откат и ссылку на дневник веса — см.
    ai_trainer.execute_tool) — тут нужна именно кнопка отката."""
    captured: list[dict] = []

    async def on_action(action: dict) -> None:
        captured.append(action)

    await ai_trainer.execute_tool(user_id, tool, payload, on_action=on_action)
    undoable = [a for a in captured if "undo" in a]
    return undoable[-1] if undoable else (captured[-1] if captured else None)


async def test_every_self_acting_tool_offers_an_undo(fresh_db, user_id):
    """Опись, а не проверка одного случая: новый инструмент, который делает
    что-то сам, не должен незаметно проехать мимо кнопки отката."""
    assert set(ai_trainer._UNDOABLE_TOOLS) == {
        "log_bodyweight", "log_food", "create_exercise", "rename_exercise",
        "move_exercise_to_group", "rename_program", "copy_program",
        "delete_food_entry", "delete_bodyweight_log",
    }
    # И ни один из них не должен заодно оказаться среди тех, что только предлагают.
    assert not set(ai_trainer._UNDOABLE_TOOLS) & set(ai_trainer._ACTION_TOOLS)


async def test_logged_bodyweight_can_be_taken_back(fresh_db, user_id):
    action = await _call(user_id, "log_bodyweight", {"weight": 78.4})

    assert action is not None and action["undo"]["kind"] == "bodyweight"
    assert len(await fresh_db.list_bodyweight_logs(user_id)) == 1

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    assert await fresh_db.list_bodyweight_logs(user_id) == []


async def test_logged_bodyweight_also_offers_a_diary_link(fresh_db, user_id):
    """Вес виден прямо в тексте ответа — на кнопке отката его дублировать не
    нужно, а вот ссылка на 🏋️ Дневник веса (график, история) — да."""
    captured: list[dict] = []

    async def on_action(action: dict) -> None:
        captured.append(action)

    await ai_trainer.execute_tool(
        user_id, "log_bodyweight", {"weight": 78.4}, on_action=on_action
    )

    assert [a["label"] for a in captured] == ["↩️ Отменить", "⚖️ Дневник веса"]
    assert captured[1]["callback"] == "menu:bodyweight"


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


async def test_profile_is_written_at_once_and_without_any_button(fresh_db, user_id):
    """Под записью в профиль не должно появиться ни одной кнопки.

    Обе прошлые попытки — откат и подтверждение — вставали рядом с
    «🗂 Забрать: <программа>» и отодвигали главное действие непонятной надписью
    про профиль. Взамен память живая: правится словами (см. соседние тесты)."""
    action = await _call(user_id, "save_athlete_profile", {"limitations": "болит плечо"})

    assert action is None
    assert (await fresh_db.get_user(user_id))["limitations"] == "болит плечо"


async def test_forget_erases_a_field_the_user_took_back(fresh_db, user_id):
    """«Колено давно не болит» — записанного больше нет, и нового значения нет."""
    await _call(user_id, "save_athlete_profile", {"limitations": "болит колено"})

    payload = json.loads(
        await ai_trainer.execute_tool(
            user_id, "save_athlete_profile", {"forget": ["limitations"]}
        )
    )

    assert payload["forgotten"] == ["limitations"]
    assert (await fresh_db.get_user(user_id))["limitations"] is None


async def test_forget_does_not_erase_what_the_same_call_just_wrote(fresh_db, user_id):
    """Модель вполне может прислать и новое значение, и forget на то же поле —
    «раньше болело колено, теперь плечо». Стереть тут значит потерять как раз
    то, что человек только что сказал."""
    payload = json.loads(
        await ai_trainer.execute_tool(
            user_id,
            "save_athlete_profile",
            {"limitations": "болит плечо", "forget": ["limitations"]},
        )
    )

    assert payload["forgotten"] == []
    assert (await fresh_db.get_user(user_id))["limitations"] == "болит плечо"


async def test_undo_keys_survive_the_json_fsm_round_trip(fresh_db, user_id):
    """Ключи намеренно не числовые: состояние FSM лежит в JSON, а
    fsm_storage._restore_int_keys превращает «7» обратно в int 7 — после
    перезапуска поиск по строке из callback_data не нашёл бы ничего."""
    import fsm_storage

    state = _state(user_id)
    buttons = await ai_handler._register_actions(
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

    out = await ai_handler._register_actions(state, [dict(original)])

    assert out == [original]
    assert not (await state.get_data()).get("ai_undo")


async def test_only_the_last_few_undos_are_kept(fresh_db, user_id):
    """Состояние FSM уезжает на диск целиком при каждой записи — копить
    описания откатов вечно незачем."""
    state = _state(user_id)
    for i in range(ai_handler._UNDO_SLOTS + 4):
        await ai_handler._register_actions(
            state, [{"label": "↩️", "undo": {"kind": "bodyweight", "id": i}}]
        )

    store = (await state.get_data())["ai_undo"]
    assert len(store) == ai_handler._UNDO_SLOTS
    # Выживают свежие, а не первые попавшиеся.
    assert "u1" not in store
    assert f"u{ai_handler._UNDO_SLOTS + 4}" in store


async def test_several_profile_writes_in_one_turn_add_no_buttons(fresh_db, user_id):
    """Сборка программы зовёт save_athlete_profile несколько раз за ход — и ни
    один из вызовов не должен ничего добавить под ответ."""
    captured: list[dict] = []

    async def on_action(action: dict) -> None:
        captured.append(action)

    for payload in ({"experience": "новичок"}, {"goal": "масса"}):
        await ai_trainer.execute_tool(
            user_id, "save_athlete_profile", payload, on_action=on_action
        )

    assert captured == []
    user = await fresh_db.get_user(user_id)
    assert (user["experience"], user["goal"]) == ("новичок", "масса")


# ---------- удаление записей веса и еды (delete_food_entry / delete_bodyweight_log) ----------


async def test_delete_food_entry_removes_it_and_undo_restores_it_whole(fresh_db, user_id):
    """Живой запрос из Claude Desktop: «удали эту запись» упирался в «у меня
    нет инструмента на удаление» — get_food_diary не отдавал entry_id, и
    убрать запись мог только человек руками в самом боте.

    Откат тут — не «намекни переделать», а честное восстановление той же
    строки: kind=food_restore в _apply_undo."""
    entry_id = await fresh_db.add_food_entry(
        user_id, eaten_on="2026-08-08", description="Протеиновый батончик",
        calories=200, protein=20, fat=7, carbs=15,
    )

    action = await _call(user_id, "delete_food_entry", {"entry_id": entry_id})

    assert action is not None and action["undo"]["kind"] == "food_restore"
    assert await fresh_db.get_food_entry(entry_id) is None

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    restored = (await fresh_db.list_food_entries(user_id, "2026-08-08"))[0]
    assert (restored["description"], restored["calories"]) == ("Протеиновый батончик", 200)


async def test_delete_food_entry_refuses_someone_elses_row(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=user_id + 1, username="other")
    entry_id = await fresh_db.add_food_entry(
        other["telegram_id"], eaten_on="2026-08-08", description="Чужое"
    )

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "delete_food_entry", {"entry_id": entry_id})
    )

    assert "error" in payload
    assert await fresh_db.get_food_entry(entry_id) is not None


async def test_delete_bodyweight_log_removes_it_and_undo_restores_it(fresh_db, user_id):
    log_id = await fresh_db.add_bodyweight_log(user_id, 78.4, logged_at="2026-08-08T09:00:00")

    action = await _call(user_id, "delete_bodyweight_log", {"log_id": log_id})

    assert action is not None and action["undo"]["kind"] == "bodyweight_restore"
    assert await fresh_db.list_bodyweight_logs(user_id) == []

    assert await ai_handler._apply_undo(user_id, action["undo"]) is not None
    logs = await fresh_db.list_bodyweight_logs(user_id)
    assert [log["weight"] for log in logs] == [78.4]


async def test_delete_bodyweight_log_refuses_someone_elses_row(fresh_db, user_id):
    other = await fresh_db.get_or_create_user(telegram_id=user_id + 1, username="other")
    log_id = await fresh_db.add_bodyweight_log(other["telegram_id"], 70.0)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "delete_bodyweight_log", {"log_id": log_id})
    )

    assert "error" in payload
    assert await fresh_db.get_bodyweight_log(log_id) is not None


# ---------- жалоба на бот, сказанная тренеру ----------


async def test_feedback_is_only_offered_never_sent(fresh_db, user_id, monkeypatch):
    """Письмо уходит наружу, живому человеку, — значит, отправляет его тап, а
    не модель. И текст письма едет в описании кнопки, а не в callback_data:
    туда он не влез бы даже обрезанным."""
    monkeypatch.setattr(config, "ADMIN_ID", 999)

    action = await _call(
        user_id,
        "send_feedback_to_admin",
        {"message": "Объём за неделю считает без суперсетов", "kind": "bug"},
    )

    assert action is not None
    assert action["label"] == "📬 Передать разработчику"
    assert action["feedback"]["text"] == "Объём за неделю считает без суперсетов"
    assert action["feedback"]["label"] == ai_trainer.FEEDBACK_KIND_LABELS["bug"]


async def test_feedback_without_text_is_refused(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", 999)

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "send_feedback_to_admin", {"message": "   "})
    )

    assert "error" in payload


async def test_feedback_with_nobody_to_send_it_to_gives_no_button(fresh_db, user_id, monkeypatch):
    """Кнопка, которая заведомо ничего не отправит, хуже её отсутствия."""
    monkeypatch.setattr(config, "ADMIN_ID", None)

    captured: list[dict] = []

    async def on_action(action: dict) -> None:
        captured.append(action)

    payload = json.loads(
        await ai_trainer.execute_tool(
            user_id, "send_feedback_to_admin", {"message": "не работает кнопка"},
            on_action=on_action,
        )
    )

    assert "error" in payload
    assert captured == []


async def test_feedback_letter_is_capped(fresh_db, user_id, monkeypatch):
    """maxLength в схеме — просьба к модели, а не гарантия; в сообщение
    Telegram письмо должно влезть вместе с шапкой."""
    monkeypatch.setattr(config, "ADMIN_ID", 999)

    action = await _call(
        user_id, "send_feedback_to_admin", {"message": "а" * (ai_trainer.FEEDBACK_MAX_LEN + 500)}
    )

    assert len(action["feedback"]["text"]) == ai_trainer.FEEDBACK_MAX_LEN


# ---------- один ход — одна кнопка отката ----------


async def test_many_undos_in_one_turn_fold_into_a_single_button(fresh_db, user_id):
    """«Удали всё из дневника еды» — это два десятка вызовов, у каждого свой
    откат. Под ответ влезало keyboards.MAX_AI_ACTIONS кнопок, то есть первые
    три: остальные записи вернуть было нечем, а тренер писал «отменишь
    кнопками отката ниже»."""
    state = _state(user_id)

    buttons = await ai_handler._register_actions(
        state,
        [
            {"label": f"↩️ Вернуть «еда {i}»", "undo": {"kind": "bodyweight", "id": i}}
            for i in range(1, 22)
        ],
    )

    assert len(buttons) == 1
    assert buttons[0]["label"] == "↩️ Отменить всё — 21 изменение"
    assert buttons[0]["is_undo"] is True
    store = (await state.get_data())["ai_undo"]
    assert len(store) == 1
    folded = next(iter(store.values()))
    assert folded["kind"] == "batch"
    assert len(folded["items"]) == 21


async def test_a_single_undo_keeps_its_own_name(fresh_db, user_id):
    """Одна правка — одна понятная кнопка: «Отменить всё» вместо «Вернуть имя
    «Жим»» скрывало бы, что именно откатывается."""
    state = _state(user_id)

    buttons = await ai_handler._register_actions(
        state, [{"label": "↩️ Вернуть имя «Жим»", "undo": {"kind": "bodyweight", "id": 1}}]
    )

    assert [b["label"] for b in buttons] == ["↩️ Вернуть имя «Жим»"]


async def test_folding_leaves_proposed_actions_where_they_were(fresh_db, user_id):
    """Откаты складываются между собой, но не съедают кнопки, которые тренер
    только предложил, — и общая встаёт на место первого отката."""
    state = _state(user_id)

    buttons = await ai_handler._register_actions(
        state,
        [
            {"label": "🗄 В архив: Жим", "callback": "rt:exarchask:5"},
            {"label": "↩️ Вернуть A", "undo": {"kind": "bodyweight", "id": 1}},
            {"label": "↩️ Вернуть B", "undo": {"kind": "bodyweight", "id": 2}},
        ],
    )

    assert [b["label"] for b in buttons] == [
        "🗄 В архив: Жим",
        "↩️ Отменить всё — 2 изменения",
    ]


async def test_folded_undo_rolls_every_change_back(fresh_db, user_id):
    """Тап по общей кнопке возвращает всё, что сделал этот ход."""
    db = fresh_db
    ids = [await db.add_food_entry(user_id, "2026-08-08", f"еда {i}") for i in range(3)]

    done = await ai_handler._apply_undo(
        user_id, {"kind": "batch", "items": [{"kind": "food", "id": i} for i in ids]}
    )

    assert done == "Вернул 3 изменения"
    assert await db.list_food_entries(user_id, "2026-08-08") == []


async def test_folded_undo_says_how_much_it_could_not_return(fresh_db, user_id):
    """Часть записей успели поправить руками — молчать об этом нельзя: человек
    решит, что вернулось всё."""
    db = fresh_db
    kept = await db.add_food_entry(user_id, "2026-08-08", "каша")

    done = await ai_handler._apply_undo(
        user_id,
        {"kind": "batch", "items": [{"kind": "food", "id": kept}, {"kind": "food", "id": 999999}]},
    )

    assert done == "Вернул 1 изменение из 2 — остальное уже правили руками"


async def test_folded_undo_reports_failure_when_nothing_came_back(fresh_db, user_id):
    """Ни одного отката — это не «вернул 0», а честное «не вышло»: обработчик
    покажет алерт вместо бодрого подтверждения."""
    done = await ai_handler._apply_undo(
        user_id,
        {"kind": "batch", "items": [{"kind": "food", "id": 999998}, {"kind": "food", "id": 999999}]},
    )

    assert done is None
