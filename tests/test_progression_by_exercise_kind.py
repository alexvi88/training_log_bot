"""Правило прогрессии и подсказка «🎯 Цель» по виду нагрузки упражнения.

Живой прогон тренера: шаг в кг стоял на каждом упражнении программы —
«Подтягивания» +2.5 кг новичку, «Скручивания» +2.5, «Планка» +5 при
«повторах» 30–45 (то есть секундах). Вид нагрузки берётся по идентичности
шаблона (seed_data.progression_kind), поэтому английское имя ведёт себя так же.
"""

import json

import ai_trainer
import analytics
import db
import formatting
import i18n
import progression_data
import seed_data
from handlers.workout import _logging_hint


def _item(name: str, progression: dict, reps=(6, 12)) -> dict:
    return ai_trainer._clean_program_item(
        {"name": name, "sets": 3, "reps_min": reps[0], "reps_max": reps[1], "progression": progression}
    )


# ---------- вид нагрузки ----------

def test_kind_is_resolved_by_identity_in_both_languages():
    assert seed_data.progression_kind("Подтягивания") == "bodyweight"
    assert seed_data.progression_kind_for_name("Pull-Ups") == "bodyweight"
    assert seed_data.progression_kind_for_name("Crunches") == "no_load"
    assert seed_data.progression_kind_for_name("Plank") == "timed"
    assert seed_data.progression_kind("Жим штанги лёжа") == "weight"
    assert seed_data.progression_kind("Моё упражнение") == "weight"


def test_every_listed_kind_is_a_real_catalog_template():
    """Опечатка в списке молча оставила бы упражнение со шагом в кг."""
    catalog = {name for _group, name in seed_data.EXERCISE_TEMPLATES}
    listed = set(seed_data.BODYWEIGHT_TEMPLATES) | seed_data.NO_LOAD_TEMPLATES | seed_data.TIMED_TEMPLATES
    assert listed <= catalog


# ---------- _clean_program_item ----------

def test_pull_ups_lose_the_kg_step_but_keep_double_progression():
    item = _item("Подтягивания", {"rule": "double_progression", "reps_top": 12, "step": 2.5})
    assert item["progression"] == {"rule": "double_progression", "reps_top": 12}
    assert any("step снят" in note for note in item["clamped"])


def test_english_pull_ups_are_treated_the_same():
    item = _item("Pull-Ups", {"rule": "double_progression", "reps_top": 12, "step": 2.5})
    assert item["progression"] == {"rule": "double_progression", "reps_top": 12}
    assert any("step снят" in note for note in item["clamped"])


def test_crunches_linear_load_becomes_double_progression_by_reps():
    item = _item("Скручивания", {"rule": "linear_load", "step": 2.5}, reps=(12, 20))
    assert item["progression"] == {"rule": "double_progression", "reps_top": 20}
    assert any("linear_load→double_progression" in note for note in item["clamped"])


def test_linear_load_without_a_range_on_bodyweight_is_dropped_with_a_note():
    item = ai_trainer._clean_program_item(
        {"name": "Подъём ног в висе", "progression": {"rule": "linear_load", "step": 1}}
    )
    assert item["progression"] is None
    assert any("отброшена" in note for note in item["clamped"])


def test_plank_keeps_seconds_and_steps_in_seconds():
    item = _item("Планка", {"rule": "double_progression", "reps_top": 45, "step": 5}, reps=(30, 45))
    assert (item["reps_min"], item["reps_max"]) == (30, 45)
    assert item["progression"] == {
        "rule": "double_progression", "reps_top": 45, "step": 5, "step_unit": "sec",
    }
    assert any("секунды" in note for note in item["clamped"])


def test_plank_step_is_clamped_to_seconds_and_defaults_to_five():
    assert _item("Планка", {"rule": "double_progression", "step": 2.5}, (30, 45))["progression"]["step"] == 5
    assert _item("Планка", {"rule": "double_progression", "step": 30}, (30, 45))["progression"]["step"] == 15
    assert _item("Plank", {"rule": "double_progression"}, (30, 45))["progression"]["step"] == 5


def test_weighted_lift_is_unchanged():
    item = _item("Жим штанги лёжа", {"rule": "double_progression", "reps_top": 10, "step": 2.5}, (6, 10))
    assert item["progression"] == {"rule": "double_progression", "reps_top": 10, "step": 2.5}
    assert item["clamped"] == []


def test_custom_exercise_outside_the_catalog_is_unchanged():
    item = _item("Мой странный тренажёр", {"rule": "linear_load", "step": 2.5})
    assert item["progression"] == {"rule": "linear_load", "step": 2.5}


async def test_propose_program_uses_identity_of_a_renamed_own_fork(fresh_db, user_id):
    """Своё переименованное «Подтягивания» — всё ещё подтягивания: вид берётся
    по original_name, а не по тому, как модель назвала упражнение."""
    ex_id = await fresh_db.get_or_create_user_exercise_by_name(user_id, "Подтягивания")
    await fresh_db.update_exercise_name(ex_id, "Турник")
    raw = await ai_trainer.execute_tool(
        user_id,
        "propose_program",
        {
            "name": "Дом",
            "description": "Дома.",
            "days": [{"name": "День 1", "exercises": [
                {"name": "Турник", "sets": 3, "reps_min": 5, "reps_max": 10,
                 "progression": {"rule": "double_progression", "reps_top": 10, "step": 2.5}},
                {"name": "Жим штанги лёжа", "sets": 3, "reps_min": 5, "reps_max": 10,
                 "progression": {"rule": "double_progression", "reps_top": 10, "step": 2.5}},
            ]}],
        },
        on_program=_noop,
    )
    payload = json.loads(raw)
    clamped = payload["days"][0]["clamped"]
    assert len(clamped) == 1 and "step снят" in clamped[0]


async def _noop(_draft):
    return None


# ---------- подсказка «🎯 Цель» ----------

def test_bodyweight_hint_ignores_a_stale_kg_step_and_chases_reps():
    rule = {"rule": "double_progression", "reps_top": 8, "step": 2.5}
    s = analytics.suggest_progression([(0, 8), (0, 7)], rule=rule, kind="bodyweight")
    assert s.action == "add_reps" and s.target_reps == 9 and s.is_bodyweight


def test_bodyweight_with_added_load_ignores_the_programs_linear_kg_step():
    rule = {"rule": "linear_load", "step": 2.5}
    s = analytics.suggest_progression([(10, 8)], rule=rule, kind="bodyweight")
    assert s.action == "add_reps" and s.target_weight == 10 and s.target_reps == 9
    # Тот же ввод у штанги — прибавка веса по правилу, как и раньше.
    s = analytics.suggest_progression([(10, 8)], rule=rule)
    assert s.action == "add_weight" and s.target_weight == 12.5


def test_timed_hint_adds_seconds_not_kilos():
    s = analytics.suggest_progression([(0, 45), (0, 40)], kind="timed")
    assert s.is_timed and s.target_weight == 0 and s.target_reps == 50
    rule = {"rule": "double_progression", "reps_top": 45, "step": 10, "step_unit": "sec"}
    assert analytics.suggest_progression([(0, 45)], rule=rule, kind="timed").target_reps == 55
    # Шаг из старого правила без step_unit — это килограммы, секундами его не считаем.
    stale = {"rule": "linear_load", "step": 2.5}
    assert analytics.suggest_progression([(0, 45)], rule=stale, kind="timed").target_reps == 50


def test_hint_text_for_bodyweight_and_timed():
    with i18n.use_lang("ru"):
        timed = progression_data.hint([(0, 45, None)], [], unit="kg", formula="epley", kind="timed")
        assert timed["text"] == "🎯 Цель: 50 сек"
        assert timed["is_timed"] is True
        bw = progression_data.hint(
            [(0, 8, None)], [], unit="kg", formula="epley",
            rule={"rule": "double_progression", "reps_top": 8, "step": 2.5}, kind="bodyweight",
        )
        assert bw["text"] == "🎯 Цель: 9 повторов"
        assert "кг" not in bw["text"]
    with i18n.use_lang("en"):
        timed = progression_data.hint([(0, 45, None)], [], unit="kg", formula="epley", kind="timed")
        assert timed["text"] == "🎯 Goal: 50 sec"


def test_live_card_shows_seconds_for_a_plank():
    text = _logging_hint(
        [(0, 40, None)], has_sets=True, show_instruction=False, progression_kind="timed"
    )
    assert "🎯 Цель: 45 сек" in text


async def test_hint_for_workout_reads_the_kind_from_identity(fresh_db, user_id):
    ex_id = await fresh_db.get_or_create_user_exercise_by_name(user_id, "Планка")
    assert await fresh_db.exercise_progression_kind(ex_id) == "timed"
    custom = await fresh_db.create_exercise(
        user_id, "Свой тренажёр", (await fresh_db.list_muscle_groups(None, global_only=True))[0]["id"]
    )
    assert await fresh_db.exercise_progression_kind(custom) == "weight"


# ---------- показ правила и смена единиц ----------

def test_timed_rule_reads_in_seconds():
    rule = {"rule": "double_progression", "reps_top": 45, "step": 5, "step_unit": "sec"}
    with i18n.use_lang("ru"):
        assert formatting.format_progression_rule(rule, "kg") == "дошёл до 45 сек — прибавь 5 сек"
    with i18n.use_lang("en"):
        assert formatting.format_progression_rule(rule, "lb") == "hit 45 sec — add 5 sec"


def test_switching_units_does_not_scale_a_seconds_step():
    draft = {"days": [{"items": [
        {"progression": {"rule": "double_progression", "step": 5, "step_unit": "sec"}},
        {"progression": {"rule": "double_progression", "step": 2.5}},
    ]}]}
    db.scale_draft_progression_steps(draft, 2.0)
    items = draft["days"][0]["items"]
    assert items[0]["progression"]["step"] == 5
    assert items[1]["progression"]["step"] == 5.0
