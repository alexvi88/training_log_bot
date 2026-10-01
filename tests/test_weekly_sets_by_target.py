"""Недельный объём программы по мишеням внутри «Плеч» и «Ног».

Живой прогон: тренер назвал «плечи 16», а на среднюю дельту там было три
подхода в неделю — даже когда атлет сказал, что плечи отстают; «ноги 13»
прятали пять подходов на квадрицепс. propose_program теперь возвращает ещё
weekly_sets_by_target, и для этого у каждого шаблона этих двух групп есть
одна основная мишень (seed_data.EXERCISE_MUSCLE_TARGET).
"""

import json

import ai_trainer
import i18n
import seed_data


def _day(name: str, exercises: list[dict]) -> dict:
    return {"name": name, "exercises": exercises}


async def _propose(user_id: int, tool_input: dict) -> dict:
    async def on_program(_draft: dict) -> None:
        return None

    raw = await ai_trainer.execute_tool(
        user_id, "propose_program", tool_input, on_program=on_program
    )
    return json.loads(raw)


# ---------- полнота разметки ----------

def test_every_shoulder_and_leg_template_has_a_target():
    missing = [
        name
        for group, name in seed_data.EXERCISE_TEMPLATES
        if group in seed_data.MUSCLE_TARGETS_BY_GROUP
        and name not in seed_data.EXERCISE_MUSCLE_TARGET
    ]
    assert missing == []


def test_every_target_belongs_to_the_templates_group():
    """Ключи — существующие шаблоны (идентичность, не показ), мишень — из
    той же группы, что и шаблон."""
    group_of = dict((name, group) for group, name in seed_data.EXERCISE_TEMPLATES)
    for name, target in seed_data.EXERCISE_MUSCLE_TARGET.items():
        assert name in group_of, name
        assert target in seed_data.MUSCLE_TARGETS_BY_GROUP[group_of[name]], (name, target)


def test_every_target_has_a_name_in_both_languages():
    for targets in seed_data.MUSCLE_TARGETS_BY_GROUP.values():
        for target in targets:
            for lang in i18n.SUPPORTED:
                shown = seed_data.localized_muscle_target_name(target, lang)
                assert shown and "muscle_target" not in shown, (target, lang)


# ---------- подсчёт ----------

async def test_shoulders_split_into_front_side_rear(fresh_db, user_id):
    days = [
        {
            "items": [
                {"name": "Жим штанги стоя", "sets": 3},
                {"name": "Разведение гантелей в стороны", "sets": 3},
                {"name": "Тяга к лицу", "sets": 2},
            ]
        }
    ]

    totals = await ai_trainer._weekly_sets_by_target(user_id, days)

    assert totals == {
        "Плечи": {"Передняя дельта": 3, "Средняя дельта": 3, "Задняя дельта": 2}
    }


async def test_a_trained_group_shows_its_empty_targets_too(fresh_db, user_id):
    """Ноль на бицепсе бедра — ровно то, что тренер должен увидеть."""
    days = [
        {"items": [{"name": "Присед со штангой", "sets": 4}]},
        {"items": [{"name": "Жим ногами", "sets": 3}, {"name": "Подъём на носки стоя", "sets": 4}]},
    ]

    totals = await ai_trainer._weekly_sets_by_target(user_id, days)

    # Приводящие без подходов не показываются: минимума у них нет.
    assert totals == {
        "Ноги": {"Квадрицепс": 7, "Бицепс бедра": 0, "Ягодицы": 0, "Икры": 4}
    }


async def test_english_names_resolve_to_the_same_identity(fresh_db, user_id):
    await fresh_db.update_user(user_id, lang="en")
    name_ru = "Разведение гантелей в стороны"
    shown = seed_data.localized_exercise_name(name_ru, "en")
    assert shown != name_ru
    days = [{"items": [{"name": shown, "sets": 4}]}]

    with i18n.use_lang("en"):
        totals = await ai_trainer._weekly_sets_by_target(user_id, days)

    assert totals == {"Shoulders": {"Front delts": 0, "Side delts": 4, "Rear delts": 0}}


async def test_custom_exercise_counts_in_group_but_not_in_targets(fresh_db, user_id):
    groups = {g["name"]: g["id"] for g in await fresh_db.list_muscle_groups(user_id)}
    await fresh_db.create_exercise(user_id, "Мои махи с резинкой", groups["Плечи"])
    days = [{"items": [{"name": "Мои махи с резинкой", "sets": 5}]}]

    assert await ai_trainer._weekly_sets_by_group(user_id, days) == {"Плечи": 5}
    assert await ai_trainer._weekly_sets_by_target(user_id, days) == {}


async def test_propose_program_returns_targets(fresh_db, user_id):
    payload = await _propose(
        user_id,
        {
            "name": "Плечи отстают",
            "days": [
                _day("A", [
                    {"name": "Жим штанги стоя", "sets": 3, "reps_min": 6, "reps_max": 10},
                    {"name": "Разведение гантелей в стороны", "sets": 3},
                ]),
                _day("B", [{"name": "Тяга к лицу", "sets": 2}]),
                _day("C", [{"name": "Жим штанги лёжа", "sets": 4}]),
            ],
        },
    )

    assert payload["weekly_sets_by_group"]["Плечи"] == 8
    assert payload["weekly_sets_by_target"] == {
        "Плечи": {"Передняя дельта": 3, "Средняя дельта": 3, "Задняя дельта": 2}
    }
    assert "weekly_sets_by_target" in payload["note"]


def test_prompt_asks_to_check_targets():
    prompt = ai_trainer.SYSTEM_PROMPT
    assert "weekly_sets_by_target" in prompt
    assert "средняя дельта, квадрицепс и бицепс бедра" in prompt
