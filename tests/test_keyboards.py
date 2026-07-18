import keyboards


def test_ai_trainer_keyboard_default_layout():
    kb = keyboards.ai_trainer_keyboard()
    rows = kb.inline_keyboard
    assert [btn.callback_data for btn in rows[0]] == ["ai:menu"]


def test_ai_trainer_keyboard_adds_resume_workout_button_when_active():
    kb = keyboards.ai_trainer_keyboard(has_active_workout=True)
    rows = kb.inline_keyboard
    assert [btn.callback_data for btn in rows[0]] == ["ai:menu", "ai:resume_workout"]


def _button_texts(kb):
    return [btn.text for row in kb.inline_keyboard for btn in row]


def test_exercise_picker_entry_keyboard_offers_finish_when_not_empty():
    kb = keyboards.exercise_picker_entry_keyboard(is_empty=False)
    assert "🏁 Завершить тренировку" in _button_texts(kb)
    assert "⬅️ В меню" not in _button_texts(kb)


def test_exercise_picker_entry_keyboard_offers_menu_exit_when_empty():
    kb = keyboards.exercise_picker_entry_keyboard(is_empty=True)
    assert "⬅️ В меню" in _button_texts(kb)
    assert "🏁 Завершить тренировку" not in _button_texts(kb)
