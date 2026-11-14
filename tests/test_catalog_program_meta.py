"""Время и оборудование готовых программ (seed_data.program_minutes /
program_equipment) — на странице программы в приложении, «подойдёт ли мне»."""

import re

import pytest

import i18n
import seed_data

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_every_catalog_exercise_has_equipment():
    used = {ex for p in seed_data.WORKOUT_PROGRAMS for _d, exs in p["days"] for ex, _t in exs}
    missing = used - set(seed_data.PROGRAM_EXERCISE_EQUIPMENT)
    assert not missing, f"добавь оборудование в PROGRAM_EXERCISE_EQUIPMENT: {sorted(missing)}"
    tags = {t for tags in seed_data.PROGRAM_EXERCISE_EQUIPMENT.values() for t in tags}
    assert tags <= set(seed_data.EQUIPMENT_ORDER)


@pytest.mark.parametrize("program", seed_data.WORKOUT_PROGRAMS, ids=lambda p: p["key"])
def test_program_minutes_are_a_plausible_workout(program):
    minutes = seed_data.program_minutes(program["key"])
    assert 20 <= minutes <= 90 and minutes % 5 == 0


def test_heavier_scheme_takes_longer_per_set():
    assert seed_data._set_minutes("5×5")[1] > seed_data._set_minutes("3×12–15")[1]
    assert seed_data._set_minutes("3×30–60 сек") == (3, 1.5)


@pytest.mark.parametrize("lang", [lang for lang in i18n.SUPPORTED if lang != "ru"])
def test_equipment_is_in_the_athletes_language(lang):
    for program in seed_data.WORKOUT_PROGRAMS:
        labels = seed_data.program_equipment(program["key"], lang)
        assert labels, program["key"]
        assert not any(CYRILLIC.search(label) for label in labels), (lang, labels)


def test_strength_program_needs_only_barbell_rack_and_bench():
    assert seed_data.program_equipment("strength5x5", "en") == ["barbell", "rack", "bench"]
