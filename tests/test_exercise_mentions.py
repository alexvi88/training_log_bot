"""exercise_mentions: finding the user's exercises inside AI-trainer prose."""

import pytest

import exercise_mentions
import keyboards


def _ex(ex_id: int, name: str, display_name: str | None = None) -> dict:
    return {"id": ex_id, "name": name, "display_name": display_name or name}


PULLDOWN = _ex(1, "Тяга верхнего блока", "Тяга верхнего блока · широкий хват")
ROW = _ex(2, "Тяга горизонтального блока")
BENCH = _ex(3, "Жим лёжа")
BENCH_CLOSE = _ex(4, "Жим лёжа узким хватом")
SQUAT = _ex(5, "Приседания со штангой")


def _names(rows) -> list[str]:
    return [r["name"] for r in rows]


def test_finds_exact_name():
    found = exercise_mentions.find_mentions("Убери тяга верхнего блока на неделю", [PULLDOWN, ROW])
    assert _names(found) == ["Тяга верхнего блока"]


def test_finds_inflected_name():
    """Тренер пишет прозой: «замени на тягу горизонтального блока»."""
    text = "Если боль остаётся — замени на тягу горизонтального блока или подтягивания."
    assert _names(exercise_mentions.find_mentions(text, [PULLDOWN, ROW])) == [
        "Тяга горизонтального блока"
    ]


def test_ignores_unmentioned_exercises():
    assert exercise_mentions.find_mentions("Ешь больше белка и спи 8 часов", [BENCH, SQUAT]) == []


def test_longer_name_wins_over_contained_one():
    found = exercise_mentions.find_mentions("Попробуй жим лёжа узким хватом", [BENCH, BENCH_CLOSE])
    assert _names(found) == ["Жим лёжа узким хватом"]


def test_keeps_order_of_appearance():
    text = "Сначала приседания со штангой, потом жим лёжа, в конце тяга верхнего блока."
    found = exercise_mentions.find_mentions(text, [BENCH, PULLDOWN, SQUAT])
    assert _names(found) == ["Приседания со штангой", "Жим лёжа", "Тяга верхнего блока"]


def test_caps_the_number_of_mentions():
    text = "Приседания со штангой, жим лёжа, тяга верхнего блока и тяга горизонтального блока."
    found = exercise_mentions.find_mentions(text, [BENCH, PULLDOWN, SQUAT, ROW])
    assert len(found) == exercise_mentions.MAX_MENTIONS
    assert _names(found)[0] == "Приседания со штангой"


def test_same_exercise_mentioned_twice_gives_one_button():
    text = "Жим лёжа не растёт. Значит, жим лёжа делаем два раза в неделю."
    assert len(exercise_mentions.find_mentions(text, [BENCH])) == 1


def test_short_words_do_not_match_by_stem():
    """«жир» не должен открывать карточку «Жим»."""
    assert exercise_mentions.find_mentions("Следи за жиром", [_ex(9, "Жим")]) == []


def test_case_and_yo_are_ignored():
    found = exercise_mentions.find_mentions("ЖИМ ЛЕЖА — база", [BENCH])
    assert _names(found) == ["Жим лёжа"]


def test_empty_text_is_safe():
    assert exercise_mentions.find_mentions("", [BENCH]) == []


@pytest.mark.asyncio
async def test_find_in_text_uses_the_users_own_exercises(fresh_db, user_id):
    ex_id = await fresh_db.create_exercise(user_id, "Жим лёжа", None)
    found = await exercise_mentions.find_in_text(user_id, "Добавь жим лёжа в начало")
    assert [r["id"] for r in found] == [ex_id]


@pytest.mark.asyncio
async def test_find_in_text_without_answer(fresh_db, user_id):
    assert await exercise_mentions.find_in_text(user_id, None) == []


def test_keyboard_puts_mentions_above_navigation():
    kb = keyboards.ai_trainer_keyboard(has_active_workout=True, exercises=[BENCH, PULLDOWN])
    rows = kb.inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["ai:excard:3"]
    assert [b.callback_data for b in rows[1]] == ["ai:excard:1"]
    assert [b.callback_data for b in rows[2]] == ["ai:menu", "ai:resume_workout"]


def test_keyboard_shortens_long_labels():
    long_name = _ex(7, "Тяга", "Тяга верхнего блока к груди широким хватом · блок")
    (button,) = keyboards.ai_trainer_keyboard(exercises=[long_name]).inline_keyboard[0]
    assert len(button.text) <= keyboards.AI_MENTION_LABEL_LIMIT + 2  # эмодзи + пробел
    assert button.text.endswith("…")


def test_keyboard_without_mentions_is_unchanged():
    kb = keyboards.ai_trainer_keyboard()
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["ai:menu"]


def test_full_display_name_picks_the_right_variant_of_the_same_exercise():
    """Два «жима лёжа» с разной оснасткой: выигрывает тот, что назван целиком."""
    barbell = _ex(10, "Жим лёжа", "Жим лёжа · штанга")
    dumbbells = _ex(11, "Жим лёжа", "Жим лёжа · гантели")
    found = exercise_mentions.find_mentions("Ставь жим лёжа гантели в начало", [barbell, dumbbells])
    assert [r["id"] for r in found] == [11]
