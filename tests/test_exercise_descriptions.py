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


def test_effective_description_prefers_users_own_text_over_template_default():
    ex = {"name": "Присед со штангой", "description": "Моя версия техники"}
    assert exercise_descriptions.effective_description(ex) == "Моя версия техники"


def test_effective_description_falls_back_to_template_default():
    ex = {"name": "Присед со штангой", "description": None}
    assert exercise_descriptions.effective_description(ex) == exercise_descriptions.get_description(
        "Присед со штангой"
    )


def test_effective_description_none_for_custom_exercise_with_no_override():
    ex = {"name": "pull down", "description": None}
    assert exercise_descriptions.effective_description(ex) is None
