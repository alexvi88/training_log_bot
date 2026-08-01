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


def test_overlapping_names_of_different_exercises_keep_only_the_longer_match():
    """«Жим лёжа» и «Жим лёжа узким хватом» — разные упражнения (разный `name`),
    а не оснастка одного и того же: их пересечение в тексте не схлопываем в две
    кнопки, оставляем только более конкретное совпадение."""
    found = exercise_mentions.find_mentions("Попробуй жим лёжа узким хватом", [BENCH, BENCH_CLOSE])
    assert _names(found) == ["Жим лёжа узким хватом"]


def test_unrelated_exercise_inside_another_ones_name_is_not_mentioned():
    """«pull down» лежит внутри «abs - pull down block», но это два разных,
    несвязанных упражнения — вторая кнопка тут не нужна (регрессия на реальном
    случае: тренер написал только про abs-упражнение, а кнопка появилась и на
    посторонний «pull down»)."""
    abs_pulldown = _ex(20, "abs - pull down block")
    unrelated_pulldown = _ex(21, "pull down")
    found = exercise_mentions.find_mentions(
        "У тебя уже есть abs - pull down block — норм вариант.",
        [abs_pulldown, unrelated_pulldown],
    )
    assert _names(found) == ["abs - pull down block"]


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


# ---------- paging through more mentions than fit on one screen ----------


def test_keyboard_shows_only_a_page_of_mentions_with_a_next_arrow():
    five = [_ex(i, f"Упражнение {i}") for i in range(1, 6)]
    kb = keyboards.ai_trainer_keyboard(exercises=five)
    rows = kb.inline_keyboard
    assert [b.callback_data for row in rows[:3] for b in row] == [
        "ai:excard:1", "ai:excard:2", "ai:excard:3",
    ]
    nav_row = rows[3]
    assert [b.text for b in nav_row] == ["➡️"]
    assert nav_row[0].callback_data == "ai:mpage:1:1,2,3,4,5"


def test_keyboard_second_page_shows_remaining_mentions_and_a_prev_arrow():
    five = [_ex(i, f"Упражнение {i}") for i in range(1, 6)]
    kb = keyboards.ai_trainer_keyboard(exercises=five, page=1)
    rows = kb.inline_keyboard
    assert [b.callback_data for row in rows[:2] for b in row] == ["ai:excard:4", "ai:excard:5"]
    nav_row = rows[2]
    assert [b.text for b in nav_row] == ["⬅️"]
    assert nav_row[0].callback_data == "ai:mpage:0:1,2,3,4,5"


def test_keyboard_with_few_mentions_has_no_paging_arrows():
    kb = keyboards.ai_trainer_keyboard(exercises=[BENCH, PULLDOWN])
    callback_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(cb.startswith("ai:mpage:") for cb in callback_datas)


def test_fully_named_variant_goes_first_but_the_other_stays():
    """Два «жима лёжа» с разной оснасткой: названный целиком стоит первым,
    второй остаётся кнопкой ниже — вдруг тренер имел в виду его."""
    barbell = _ex(10, "Жим лёжа", "Жим лёжа · штанга")
    dumbbells = _ex(11, "Жим лёжа", "Жим лёжа · гантели")
    found = exercise_mentions.find_mentions("Ставь жим лёжа гантели в начало", [barbell, dumbbells])
    assert [r["id"] for r in found] == [11, 10]
