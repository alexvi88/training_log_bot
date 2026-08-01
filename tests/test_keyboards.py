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


def test_exercise_picker_entry_keyboard_offers_recent_exercises_one_per_row():
    kb = keyboards.exercise_picker_entry_keyboard(recent=[(5, "Pull down"), (6, "Seated row")])
    texts = _button_texts(kb)
    assert "Pull down" in texts
    assert "Seated row" in texts
    cbs = _callback_datas(kb)
    assert "live:suggest:5" in cbs and "live:suggest:6" in cbs
    recent_rows = [
        row for row in kb.inline_keyboard
        if any(b.callback_data.startswith("live:suggest:") for b in row)
    ]
    assert len(recent_rows) == 2
    assert all(len(row) == 1 for row in recent_rows)


def test_exercise_picker_entry_keyboard_names_the_suggested_exercise():
    kb = keyboards.exercise_picker_entry_keyboard(suggested=(7, "leg press"))
    # No emoji prefix — the buttons are a plain column of exercise names.
    assert "leg press" in _button_texts(kb)
    assert "live:suggest:7" in _callback_datas(kb)


def test_exercise_picker_entry_keyboard_shortens_a_long_suggested_name():
    # Full name is kept, cut down word-boundary-aware with an ellipsis only
    # when it doesn't fit — not shortened to whatever's after a "-" qualifier.
    kb = keyboards.exercise_picker_entry_keyboard(
        suggested=(7, "biceps curls - alternating dumbbells")
    )
    label = next(
        b.text for row in kb.inline_keyboard for b in row if b.callback_data == "live:suggest:7"
    )
    assert label == "biceps curls - alternating…"


def test_exercise_picker_entry_keyboard_keeps_a_normal_length_name_whole():
    # A real exercise name fits a full-width button — it used to lose half of
    # itself to a 20-char cut made for a label that also carried an emoji.
    kb = keyboards.exercise_picker_entry_keyboard(suggested=(7, "bench press - flat - machine"))
    assert "bench press - flat - machine" in _button_texts(kb)


def test_exercise_picker_entry_keyboard_no_recent_row_when_none_given():
    kb = keyboards.exercise_picker_entry_keyboard(recent=None)
    assert not any(cb.startswith("live:suggest:") for cb in _callback_datas(kb))


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


def test_routine_detail_keyboard_has_no_per_exercise_delete_rows():
    """The 🗑-per-exercise rows used to sit directly under "▶️ Начать тренировку",
    so a mistap on the way into a session silently dropped an exercise."""
    kb = keyboards.routine_detail_keyboard(7)
    cbs = _callback_datas(kb)
    assert not any(cb.startswith("rt:rmex:") for cb in cbs)
    assert "rt:start:7" in cbs
    assert "rt:edit:7" in cbs
    # The one destructive action left on this screen asks for confirmation.
    assert "rt:delask:7" in cbs


def test_routine_edit_keyboard_carries_the_removals():
    kb = keyboards.routine_edit_keyboard(7, [(11, "Жим"), (12, "Тяга")])
    cbs = _callback_datas(kb)
    assert "rt:rmex:7:11" in cbs
    assert "rt:rmex:7:12" in cbs
    assert "rt:addex:7" in cbs
    assert "rt:view:7" in cbs  # "готово" back to the program screen


def test_edit_workout_keyboard_is_one_row_per_exercise():
    kb = keyboards.edit_workout_keyboard([(1, 10, "Становая · 3 сета"), (2, 11, "Тяга · 2 сета")])
    rows = kb.inline_keyboard
    assert len(rows) == 5  # 2 exercises + новое/дата/готово
    assert _callback_datas(kb)[:2] == ["editw:ex:1:10", "editw:ex:2:11"]


def test_edit_exercise_keyboard_lists_sets_and_a_way_back():
    kb = keyboards.edit_exercise_keyboard(1, 10, [(100, "1) 190×5"), (101, "2) 190×5")])
    cbs = _callback_datas(kb)
    assert "editw:set:100" in cbs and "editw:set:101" in cbs
    assert "editw:addset:1:10" in cbs
    assert "editw:rmexask:1" in cbs  # confirmed removal, not the bare rmex
    assert "editw:top" in cbs


def test_bodyweight_periods_match_the_progress_chart_shape():
    """Both period pickers offer the same 10/20/all shape, and neither defaults
    to the narrowest window."""
    assert [value for value, _ in keyboards.BODYWEIGHT_PERIODS] == [10, 20, 0]
    assert keyboards.DEFAULT_BODYWEIGHT_WEEKS == 20


def test_bodyweight_keyboard_marks_the_active_period():
    kb = keyboards.bodyweight_keyboard(has_logs=True, weeks=20, show_periods=True)
    texts = _button_texts(kb)
    assert "• 20 нед •" in texts
    assert "10 нед" in texts and "Всё" in texts
