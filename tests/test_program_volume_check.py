"""propose_program сверяет недельный объём черновика с методикой кодом.

Живой прогон: средняя дельта получала 2-3 подхода в неделю, а отстающие
«Плечи» после нескольких попыток модели — 17 при потолке 16. Теперь
расхождение называется модели в ответе инструмента (volume_check), черновик
при этом всё равно показан атлету.
"""

import json

import ai_trainer
import i18n


def _day(name: str, exercises: list[dict]) -> dict:
    return {"name": name, "exercises": exercises}


def _ex(name: str, sets: int, **extra) -> dict:
    return {"name": name, "sets": sets, "reps_min": 8, "reps_max": 12, **extra}


async def _propose(user_id: int, days: list[dict]) -> dict:
    async def on_program(_draft: dict) -> None:
        return None

    raw = await ai_trainer.execute_tool(
        user_id,
        "propose_program",
        {"name": "Масса", "description": "Верх и низ.", "days": days},
        on_program=on_program,
    )
    return json.loads(raw)


def _compliant_days() -> list[dict]:
    return [
        _day("Верх", [
            _ex("Жим штанги лёжа", 4),
            _ex("Жим штанги стоя", 3),
            _ex("Разведение гантелей в стороны", 3),
            _ex("Подъём штанги на бицепс", 2),
            _ex("Французский жим", 2),
        ]),
        _day("Низ", [
            _ex("Присед со штангой", 3),
            _ex("Румынская тяга", 3),
            _ex("Хип-траст со штангой", 2),
        ]),
        _day("Верх 2", [
            _ex("Разведение гантелей в стороны", 3),
            _ex("Подъём штанги на бицепс", 2),
            _ex("Французский жим", 2),
        ]),
        _day("Низ 2", [
            _ex("Жим ногами", 3),
            _ex("Сгибание ног в тренажёре", 3),
            _ex("Хип-траст со штангой", 2),
        ]),
    ]


async def test_low_side_delts_get_a_hint_and_the_draft_is_still_shown(fresh_db, user_id):
    days = _compliant_days()
    days[2]["exercises"] = [e for e in days[2]["exercises"] if "стороны" not in e["name"]]

    payload = await _propose(user_id, days)

    assert payload["shown_to_user"] is True
    assert payload["volume_check"] == ["Средняя дельта: подходов в неделю 3, нужно от 6"]
    assert "propose_program" in payload["volume_check_note"]


async def test_group_above_ceiling_gets_a_hint(fresh_db, user_id):
    days = _compliant_days()
    days[0]["exercises"].append(_ex("Жим Арнольда", 4))
    days[2]["exercises"].append(_ex("Тяга к лицу", 4))

    payload = await _propose(user_id, days)

    assert payload["weekly_sets_by_group"]["Плечи"] == 17
    assert payload["volume_check"] == ["Плечи: подходов в неделю 17, потолок 16"]


async def test_compliant_draft_has_no_hint(fresh_db, user_id):
    payload = await _propose(user_id, _compliant_days())

    assert "volume_check" not in payload
    assert "volume_check_note" not in payload


async def test_legs_are_capped_per_target_not_per_group(fresh_db, user_id):
    """Ноги 16+ на группу — норма (одни минимумы дают 16); потолок — у мишени."""
    days = _compliant_days()
    days[3]["exercises"].append(_ex("Подъём на носки стоя", 4))

    payload = await _propose(user_id, days)

    assert payload["weekly_sets_by_group"]["Ноги"] > ai_trainer.PROGRAM_MAX_WEEKLY_SETS
    assert "volume_check" not in payload


async def test_single_day_workout_is_not_checked(fresh_db, user_id):
    """Тренировка «на сегодня» — один день, недельный объём к ней не про это."""
    payload = await _propose(user_id, [_day("Сегодня", [_ex("Разведение гантелей в стороны", 2)])])

    assert "volume_check" not in payload


async def test_strength_program_skips_minimums(fresh_db, user_id):
    days = _compliant_days()
    days[2]["exercises"] = [e for e in days[2]["exercises"] if "стороны" not in e["name"]]
    days[1]["exercises"][0]["progression"] = {
        "rule": "top_set_backoff", "top_reps_min": 3, "top_reps_max": 5,
        "backoff_sets": 2, "backoff_pct": 85,
    }

    payload = await _propose(user_id, days)

    assert "volume_check" not in payload


async def test_hint_names_match_the_athletes_language(fresh_db, user_id):
    await fresh_db.update_user(user_id, lang="en")
    days = _compliant_days()
    days[2]["exercises"] = [e for e in days[2]["exercises"] if "стороны" not in e["name"]]

    with i18n.use_lang("en"):
        payload = await _propose(user_id, days)

    assert payload["volume_check"] == ["Side delts: подходов в неделю 3, нужно от 6"]


def test_prompt_numbers_come_from_the_constants():
    prompt = ai_trainer.SYSTEM_PROMPT
    mins = ai_trainer.PROGRAM_TARGET_MIN_SETS
    # Промпт называет одно число на три мишени и одно на бицепс с трицепсом.
    assert mins["side_delts"] == mins["quads"] == mins["hamstrings"]
    groups = ai_trainer.PROGRAM_GROUP_MIN_SETS
    assert groups["Бицепс"] == groups["Трицепс"]
    assert (
        f"меньше {mins['side_delts']} рабочих подходов; бицепс и трицепс — не меньше "
        f"{groups['Бицепс']}; ягодицы — не меньше {mins['glutes']}"
    ) in prompt
    assert f"до {ai_trainer.PROGRAM_MAX_WEEKLY_SETS} подходов" in prompt
