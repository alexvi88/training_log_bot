import exercise_descriptions
from seed_data import EXERCISE_TEMPLATES


def test_description_names_match_a_real_template():
    template_names = {ex_name for _group, ex_name in EXERCISE_TEMPLATES}
    for ex_name in exercise_descriptions.EXERCISE_DESCRIPTIONS:
        assert ex_name in template_names, f"{ex_name!r} is not in EXERCISE_TEMPLATES"


def test_every_template_has_a_description():
    template_names = {ex_name for _group, ex_name in EXERCISE_TEMPLATES}
    for ex_name in template_names:
        assert ex_name in exercise_descriptions.EXERCISE_DESCRIPTIONS, f"{ex_name!r} has no description"


def test_get_description_returns_text_for_known_exercise():
    assert exercise_descriptions.get_description("Присед со штангой")


def test_get_description_returns_none_for_unknown_exercise():
    assert exercise_descriptions.get_description("Совсем не упражнение") is None
