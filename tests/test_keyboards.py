import keyboards


def test_persistent_menu_carries_a_static_input_placeholder():
    """Подсказка живёт в input_field_placeholder носителя, а не четвёртой
    кнопкой, и укладывается в лимит Telegram (64 символа)."""
    kb = keyboards.persistent_menu()
    assert kb.input_field_placeholder
    assert "100 8" in kb.input_field_placeholder
    assert len(kb.input_field_placeholder) <= 64
    assert len(kb.keyboard) == 1 and len(kb.keyboard[0]) == 3


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
    assert "🏠 Меню" not in _button_texts(kb)


def test_exercise_picker_entry_keyboard_offers_menu_exit_when_empty():
    kb = keyboards.exercise_picker_entry_keyboard(is_empty=True)
    assert "🏠 Меню" in _button_texts(kb)
    assert "🏁 Завершить тренировку" not in _button_texts(kb)


def test_csv_import_page_keyboard_says_load_all_without_duplicates():
    """Без дублей загружаемое число и так видно в заголовке экрана — кнопка с
    тем же числом («Загрузить 2») просто повторяла его, а не добавляла смысл."""
    kb = keyboards.csv_import_page_keyboard(page=0, total_pages=1, new_count=2, dup_count=0)
    assert "✅ Загрузить всё" in _button_texts(kb)
    assert not any("Загрузить 2" in t for t in _button_texts(kb))


def test_csv_import_page_keyboard_names_the_count_when_some_are_duplicates():
    """С дублями «всё» уже занято соседней кнопкой (включая дубли) — здесь
    число обязано остаться, иначе непонятно, сколько загрузится «как обычно»."""
    kb = keyboards.csv_import_page_keyboard(page=0, total_pages=1, new_count=2, dup_count=1)
    assert "✅ Загрузить новые (2)" in _button_texts(kb)
    assert "⚠️ Загрузить всё (3), с дублями" in _button_texts(kb)


def test_csv_import_page_keyboard_paginates_by_page_not_by_workout():
    kb = keyboards.csv_import_page_keyboard(page=0, total_pages=3, new_count=20, dup_count=0)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "imp:page:1" in callbacks
    assert "imp:page:-1" not in callbacks


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
    assert "🏠 Меню" in _button_texts(kb)


def test_routine_detail_keyboard_has_no_per_exercise_delete_rows():
    """The 🗑-per-exercise rows used to sit directly under "▶️ Начать тренировку",
    so a mistap on the way into a session silently dropped an exercise."""
    kb = keyboards.routine_detail_keyboard(7)
    cbs = _callback_datas(kb)
    assert not any(cb.startswith("rt:rmex:") for cb in cbs)
    assert "rt:start:7" in cbs
    assert "rt:editmenu:7" in cbs
    # The one destructive action left on this screen asks for confirmation.
    assert "rt:delask:7" in cbs


def test_routine_edit_menu_keyboard_offers_composition_and_rename():
    kb = keyboards.routine_edit_menu_keyboard(7)
    cbs = _callback_datas(kb)
    assert "rt:edit:7" in cbs
    assert "rt:rename:7" in cbs
    assert "rt:view:7" in cbs


def test_routine_edit_keyboard_carries_the_removals():
    kb = keyboards.routine_edit_keyboard(7, [(11, "Жим"), (12, "Тяга")])
    cbs = _callback_datas(kb)
    assert "rt:rmex:7:11" in cbs
    assert "rt:rmex:7:12" in cbs
    assert "rt:addex:7" in cbs
    assert "rt:view:7" in cbs  # "готово" back to the program screen


def test_routine_edit_keyboard_arms_only_the_targeted_row():
    """`armed_re_id` swaps just that row's 🗑 for a "❗ Точно?" bound to
    rt:rmexyes — the second tap of the two-tap confirm — leaving every other
    row's plain 🗑 (rt:rmex) untouched."""
    kb = keyboards.routine_edit_keyboard(7, [(11, "Жим"), (12, "Тяга")], armed_re_id=11)
    buttons = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
    assert buttons["rt:rmexyes:7:11"] == "❗ Точно?"
    assert "rt:rmex:7:11" not in buttons
    assert buttons["rt:rmex:7:12"] == "🗑"


def test_routine_edit_keyboard_has_one_cyclic_arrow_per_row():
    """Стрелка только «наверх» и работает по кругу: у первого она отправляет в
    конец. Вторая колонка отбирала место у названия (оно сжималось до
    «Пр…×5–10»), а без переноса через край первое упражнение не сдвинуть."""
    kb = keyboards.routine_edit_keyboard(7, [(11, "Жим"), (12, "Тяга"), (13, "Присед")])
    cbs = _callback_datas(kb)
    assert "rt:mvex:7:11:up" in cbs
    assert "rt:mvex:7:12:up" in cbs
    assert "rt:mvex:7:13:up" in cbs
    assert not [c for c in cbs if c.endswith(":down")], "вниз больше не двигаем"


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


def test_progress_period_labels_say_workouts_not_bare_numbers():
    """Bare "10"/"20" read as unlabeled numbers next to bodyweight's "10 нед" —
    both period pickers should say what they're counting."""
    kb = keyboards.progress_chart_keyboard(exercise_id=1, limit=20, origin="all")
    texts = _button_texts(kb)
    assert "10 трен." in texts
    assert "• 20 трен. •" in texts  # active period marked
    assert "Все" in texts


def test_progress_card_and_back_buttons_carry_the_origin():
    """"📋 Карточка упражнения" must remember where the progress screen itself
    was opened from, so its own "⬅️ Назад" can return there instead of the
    exercises list (see handlers/exercises.py._exercise_detail_payload)."""
    kb = keyboards.progress_chart_keyboard(exercise_id=1, limit=20, origin="7")
    cbs = _callback_datas(kb)
    assert "prog:card:1:7" in cbs
    assert "prog:grp:7" in cbs


def test_bodyweight_periods_match_the_progress_chart_shape():
    """Both period pickers offer the same 10/20/all shape, and neither defaults
    to the narrowest window."""
    assert [value for value, _ in keyboards.BODYWEIGHT_PERIODS] == [10, 20, 0]
    assert keyboards.DEFAULT_BODYWEIGHT_WEEKS == 20


def test_bodyweight_keyboard_marks_the_active_period():
    kb = keyboards.bodyweight_keyboard(has_logs=True, weeks=20, show_periods=True)
    texts = _button_texts(kb)
    assert "• 20 нед •" in texts
    assert "10 нед" in texts and "Все" in texts


def test_bodyweight_keyboard_offers_the_records_list_when_there_are_logs():
    kb = keyboards.bodyweight_keyboard(has_logs=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bw:list:0" in cbs


def test_bodyweight_keyboard_hides_the_records_list_without_logs():
    kb = keyboards.bodyweight_keyboard(has_logs=False)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bw:list:0" not in cbs


def _bw_rows(*ids):
    return [{"id": i, "logged_at": "2026-03-14T09:00:00", "weight": 82.5} for i in ids]


def test_bodyweight_list_keyboard_numbers_delete_buttons_and_pages():
    kb = keyboards.bodyweight_list_keyboard(_bw_rows(101, 102, 103), "kg", page=1, has_next=True)
    texts = _button_texts(kb)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any("14.03" in t for t in texts)
    assert "🗑" in texts
    assert "bw:editrec:101:1" in cbs
    assert "bw:delrec:101:1" in cbs
    assert "bw:list:0" in cbs  # предыдущая страница
    assert "bw:list:2" in cbs  # следующая страница
    assert "menu:bodyweight" in cbs


def test_bodyweight_list_keyboard_no_delete_row_when_empty():
    kb = keyboards.bodyweight_list_keyboard([], "kg", page=0, has_next=False)
    texts = _button_texts(kb)
    assert not any(t.startswith("🗑") for t in texts)
