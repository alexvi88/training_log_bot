"""Три находки живого QA-прогона по чеклисту (20.08).

Все три — про ответ бота на ввод, который он сам же и предлагает: явные единицы,
вес в дневнике и название группы мышц у англоязычного атлета.
"""

import datetime as dt

import pytest

import formatting
import i18n
from handlers import workout
from parser import ParsedSet, ParseError, parse_bodyweight_entry, parse_sets_line

# ---------- 1. явные единицы снимают вопрос про перепутанные числа ----------


def _prompt(parsed, *, last_session=None, today_sets=None):
    data = {"last_session_sets": {1: last_session} if last_session else {}}
    resolved = workout._resolve_parsed_weights(data, 1, parsed)
    return workout._weight_confirm_prompt(data, 1, resolved, "kg", today_sets or [])


def test_explicit_units_are_logged_without_the_mix_up_question():
    """«15 kg / 90 reps» — роли чисел названы словами. Спрашивать «не перепутал
    ли вес и повторы» после такого ввода — отвечать на незаданный вопрос."""
    assert _prompt(parse_sets_line("15 kg / 90 reps")) is None
    assert _prompt(parse_sets_line("15kg 90")) is None
    assert _prompt(parse_sets_line("100 кг 90 раз")) is None


def test_numbers_without_units_still_get_asked():
    """Порядок чисел — единственное, что задаёт роли, и промах тут обычное дело
    («восемь по сто» голосом → 8×100)."""
    prompt = _prompt(parse_sets_line("6x60"))
    assert prompt is not None
    assert "6×60" in prompt


def test_bare_reps_with_a_label_are_not_a_mix_up_either():
    """«90 reps» после подхода на 15 кг: вес подставится с прошлого, но роль
    числа человек назвал сам."""
    data = {"last_by_exercise": {1: (15.0, 10)}, "last_session_sets": {}}
    resolved = workout._resolve_parsed_weights(data, 1, parse_sets_line("90 reps"))
    assert resolved[0].weight == 15.0
    assert workout._weight_confirm_prompt(data, 1, resolved, "kg", []) is None


def test_explicit_units_do_not_switch_off_the_weight_check():
    """Проверка веса — про величину на фоне истории, а не про порядок чисел:
    её явные единицы отменять не должны."""
    prompt = _prompt(
        parse_sets_line("500 kg x 5"), last_session=[(100.0, 8, None)]
    )
    assert prompt is not None


def test_unit_explicit_flag_travels_through_weight_resolution():
    (parsed,) = parse_sets_line("10 Kg x 4")
    assert parsed.unit_explicit is True
    (plain,) = parse_sets_line("10 4")
    assert plain.unit_explicit is False
    resolved = workout._resolve_parsed_weights({}, 1, [parsed])
    assert resolved[0].unit_explicit is True
    assert isinstance(resolved[0], ParsedSet)


# ---------- 2. дневник веса отвечает про вес, а не про дату ----------


def test_own_unit_next_to_the_weight_is_accepted():
    """«82 kg» у человека с килограммами — тот же самый вес. Раньше «kg» уходило
    в разбор даты, и на ввод веса бот отвечал «не понял дату, пиши ДД.ММ.ГГГГ»."""
    assert parse_bodyweight_entry("82 kg", unit="kg") == (82.0, None)
    assert parse_bodyweight_entry("82.5кг", unit="kg") == (82.5, None)
    assert parse_bodyweight_entry("180 lb", unit="lb") == (180.0, None)


def test_a_foreign_unit_is_refused_with_a_word_about_units():
    """«180 lbs» при килограммах молча записать нельзя: 180 внутри
    человеческого диапазона, никакая проверка правдоподобия это не поймает."""
    with pytest.raises(ParseError) as err:
        parse_bodyweight_entry("180 lbs", unit="kg")
    assert "килограмм" in err.value.message
    assert "ДД.ММ" not in err.value.message


def test_the_date_still_works_next_to_a_unit():
    assert parse_bodyweight_entry("82.5 kg 01.08.2026", unit="kg") == (
        82.5, dt.date(2026, 8, 1)
    )


# ---------- 3. группа мышц на языке атлета ----------


async def test_recovery_line_names_the_group_in_the_users_language(fresh_db, user_id):
    """Живой QA: «Still recovering: • грудь — 0% recovered» на английском
    аккаунте. Строка звала .lower() на сырое имя из базы, потому что общий
    рендер отдаёт капс."""
    await fresh_db.update_user(user_id, lang="en")
    (group,) = [g for g in await fresh_db.list_muscle_groups(user_id) if g["name"] == "Грудь"]
    ex_id = await fresh_db.create_exercise(user_id, "Bench", group["id"])
    today = dt.date.today()
    workout_id = await fresh_db.create_finished_workout(
        user_id,
        dt.datetime.combine(today, dt.time(10)).isoformat(timespec="seconds"),
        dt.datetime.combine(today, dt.time(11)).isoformat(timespec="seconds"),
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(6):
        await fresh_db.add_set(block_id, ex_id, 1, 0, 100.0, 8)

    with i18n.use_lang("en"):
        line = await workout._recovery_line(user_id, await fresh_db.list_muscle_groups(user_id))

    assert "chest" in line
    assert "грудь" not in line


def test_format_group_lower_localizes_and_lowercases():
    with i18n.use_lang("en"):
        assert formatting.format_group_lower("Грудь") == "chest"
    with i18n.use_lang("ru"):
        assert formatting.format_group_lower("Грудь") == "грудь"
    # Своя группа пользователя слага не имеет и возвращается как есть.
    with i18n.use_lang("en"):
        assert formatting.format_group_lower("Предплечья") == "предплечья"
