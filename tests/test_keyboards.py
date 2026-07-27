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


def test_exercise_picker_entry_keyboard_offers_recent_exercises_in_one_row():
    kb = keyboards.exercise_picker_entry_keyboard(recent=[(5, "Pull down"), (6, "Seated row")])
    texts = _button_texts(kb)
    assert "🕘 Pull down" in texts
    assert "🕘 Seated row" in texts
    cbs = _callback_datas(kb)
    assert "live:suggest:5" in cbs and "live:suggest:6" in cbs


def test_exercise_picker_entry_keyboard_no_recent_row_when_none_given():
    kb = keyboards.exercise_picker_entry_keyboard(recent=None)
    assert not any(t.startswith("🕘") for t in _button_texts(kb))


def _callback_datas(kb):
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_exercises_keyboard_offers_templates_alongside_own_matches():
    owned = [{"id": 1, "display_name": "Моя вариация жима"}]
    templates = [{"id": 42, "display_name": "Жим штанги лёжа"}]
    kb = keyboards.exercises_keyboard(owned, prefix="pick", templates=templates)

    texts = _button_texts(kb)
    assert "Моя вариация жима" in texts
    assert "📋 Жим штанги лёжа" in texts, "a template match should be visually marked, not blend in"
    # Tapping it forks-then-opens exactly like picking it from the template
    # browser (pick:tpladd:<id>), not the plain pick:ex:<id> used for owned ones.
    assert "pick:tpladd:42" in _callback_datas(kb)


def test_exercises_keyboard_without_templates_is_unchanged():
    owned = [{"id": 1, "display_name": "Присед"}]
    kb = keyboards.exercises_keyboard(owned, prefix="pick")
    assert "pick:tpladd:" not in "".join(_callback_datas(kb))


def test_history_item_keyboard_packs_core_actions_into_two_rows():
    kb = keyboards.history_item_keyboard(7)
    rows = kb.inline_keyboard
    assert len(rows) == 2
    assert len(rows[0]) == 2 and len(rows[1]) == 2
    texts = _button_texts(kb)
    assert "🖼 Картинка" in texts
    assert "Поделиться картинкой" not in "".join(texts)


def test_history_item_keyboard_ai_button_gets_its_own_row():
    kb = keyboards.history_item_keyboard(7, show_ai_button=True)
    rows = kb.inline_keyboard
    assert len(rows) == 3
    assert [b.text for b in rows[0]] == ["🤖 Комментарий AI-тренера"]


def test_workout_card_keyboard_packs_actions_into_one_row():
    kb = keyboards.workout_card_keyboard(7)
    rows = kb.inline_keyboard
    assert len(rows) == 2
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2
    assert "🖼 Картинка" in _button_texts(kb)
    assert "✏️ Редактировать" in _button_texts(kb)
    assert "⬅️ В меню" in _button_texts(kb)
