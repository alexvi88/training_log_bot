"""Главное меню: кнопка «📥 Перенести историю из Hevy/Strong» на пустом дневнике
(задание №2) заменила «✍️ Записать прошлую тренировку» (задание №4, флоу
целиком снесён) — тот же самый механизм show_quick_log/show_import_button, и
тот же самый флоу, что и «📥 Импорт CSV» из настроек (settings:import)."""
import keyboards
from handlers import workout


def _button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_import_button_shown_on_empty_diary():
    markup = keyboards.main_menu(has_active_workout=False, show_import_button=True)
    (button,) = [
        b for row in markup.inline_keyboard for b in row if "Перенести историю" in b.text
    ]
    assert button.callback_data == "settings:import"


def test_import_button_hidden_with_history():
    markup = keyboards.main_menu(has_active_workout=False, show_import_button=False)
    assert not any("Перенести историю" in text for text in _button_texts(markup))


def test_quicklog_flow_is_gone():
    """№4: флоу и кнопка «✍️ Записать прошлую тренировку» больше не существуют."""
    assert not hasattr(workout, "menu_quick_log")
    assert not hasattr(workout, "quick_log_entered")
    assert not any("Записать прошлую тренировку" in text for text in _button_texts(
        keyboards.main_menu(has_active_workout=False, show_import_button=True)
    ))


async def test_main_menu_kb_offers_import_only_while_diary_is_empty(fresh_db, user_id):
    db = fresh_db
    markup = await workout._main_menu_kb(user_id, active=None)
    assert any("Перенести историю" in text for text in _button_texts(markup))

    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5, None)
    await db.finish_workout(workout_id)

    markup = await workout._main_menu_kb(user_id, active=None)
    assert not any("Перенести историю" in text for text in _button_texts(markup))
