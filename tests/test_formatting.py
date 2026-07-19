"""Text-formatting helpers beyond the dashboard (covered separately in test_dashboard.py)."""

import datetime as dt

import analytics
import formatting
from formatting import ExerciseBlockView

# ---------- low-level formatters ----------


def test_format_weight_drops_trailing_zero():
    assert formatting.format_weight(100.0) == "100"
    assert formatting.format_weight(100.5) == "100.5"
    assert formatting.format_weight(100.50) == "100.5"


def test_format_set():
    assert formatting.format_set(100.0, 8) == "100×8"


def test_format_date_ru_includes_weekday():
    d = dt.datetime(2026, 6, 26)  # Friday
    assert formatting.format_date_ru(d) == "26.06.2026 (пт)"


def test_format_duration_minutes_only():
    assert formatting.format_duration(45 * 60) == "45 мин"


def test_format_duration_hours_and_minutes():
    assert formatting.format_duration(75 * 60) == "1 ч 15 мин"


def test_format_duration_whole_hours():
    assert formatting.format_duration(120 * 60) == "2 ч"


# ---------- build_workout_summary ----------


def test_build_workout_summary_weighted_exercise_shows_e1rm():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(group_name="грудь", exercise_name="Жим лёжа", sets=[(100.0, 8), (100.0, 6)])
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "Жим лёжа [ГРУДЬ]" in text
    assert "100×8" in text and "100×6" in text
    assert "e1RM" in text


def test_build_workout_summary_bodyweight_exercise_shows_total_reps():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [ExerciseBlockView(group_name="пресс", exercise_name="Пресс", sets=[(0.0, 20), (0.0, 15)])]
    text = formatting.build_workout_summary(started, blocks)
    assert "повторов всего 35" in text


def test_build_workout_summary_hides_extra_stats_when_disabled():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [ExerciseBlockView(group_name="грудь", exercise_name="Жим лёжа", sets=[(100.0, 8)])]
    text = formatting.build_workout_summary(started, blocks, show_extra_stats=False)
    assert "e1RM" not in text


def test_build_workout_summary_includes_note():
    started = dt.datetime(2026, 6, 26, 18, 0)
    text = formatting.build_workout_summary(started, [], note="Болело плечо")
    assert "📝 Болело плечо" in text


def test_build_workout_summary_shows_duration_when_given():
    started = dt.datetime(2026, 6, 26, 18, 0)
    text = formatting.build_workout_summary(started, [], duration_seconds=75 * 60)
    assert "26.06.2026 (пт)</b> · 1 ч 15 мин" in text


def test_build_workout_summary_omits_duration_when_none():
    started = dt.datetime(2026, 6, 26, 18, 0)
    text = formatting.build_workout_summary(started, [], duration_seconds=None)
    assert "·" not in text.splitlines()[0]


def test_build_workout_summary_shows_previous_session_sets():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="грудь",
            exercise_name="Жим лёжа",
            sets=[(100.0, 8)],
            prev_sets=[(95.0, 8)],
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "[прошлая: 95×8]" in text


def test_build_workout_summary_italicizes_previous_session_in_history():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="грудь",
            exercise_name="Жим лёжа",
            sets=[(100.0, 8)],
            prev_sets=[(95.0, 8)],
        )
    ]
    text = formatting.build_workout_summary(started, blocks, italic_prev=True)
    assert "<i>  [прошлая: 95×8]</i>" in text


# ---------- markdown_bold_to_html ----------


def test_markdown_bold_to_html_converts_pairs():
    assert formatting.markdown_bold_to_html("**pull down**") == "<b>pull down</b>"


def test_markdown_bold_to_html_leaves_unmatched_star_pair_as_literal():
    # Simulates a ** pair split across two Telegram chunks: neither half should
    # produce an unclosed <b> tag.
    assert formatting.markdown_bold_to_html("**pull down") == "**pull down"


# ---------- build_ai_comment_block ----------


def test_build_ai_comment_block_converts_double_star_to_bold():
    text = formatting.build_ai_comment_block("Хороший прогресс на **conventional deadlift**.")
    assert "<b>conventional deadlift</b>" in text
    assert "**" not in text


def test_build_ai_comment_block_escapes_html_outside_bold():
    text = formatting.build_ai_comment_block("Тест <script> & **pull down**.")
    assert "&lt;script&gt;" in text
    assert "&amp;" in text
    assert "<b>pull down</b>" in text
    assert "<script>" not in text


def test_build_ai_comment_block_escapes_html_inside_bold():
    text = formatting.build_ai_comment_block("**A & B**")
    assert "<b>A &amp; B</b>" in text


# ---------- build_live_session_text ----------


def test_build_live_session_text_empty_no_hint():
    assert formatting.build_live_session_text([]) == "Добавь упражнение, чтобы начать."


def test_build_live_session_text_empty_with_hint():
    text = formatting.build_live_session_text([], hint="Введи вес и повторы")
    assert text == "Введи вес и повторы"


def test_build_live_session_text_marks_active_exercise():
    blocks = [
        ExerciseBlockView(group_name="грудь", exercise_name="Жим", sets=[(100.0, 8)], exercise_id=1),
        ExerciseBlockView(group_name="спина", exercise_name="Тяга", sets=[(80.0, 10)], exercise_id=2),
    ]
    text = formatting.build_live_session_text(blocks, active_exercise_id=2)
    lines = text.splitlines()
    assert any(line == "▶ <b>Тяга</b>" for line in lines)
    assert any(line == "<b>Жим</b>" for line in lines)


def test_build_live_session_text_appends_hint_after_divider():
    blocks = [ExerciseBlockView(group_name="грудь", exercise_name="Жим", sets=[(100.0, 8)])]
    text = formatting.build_live_session_text(blocks, hint="Что дальше?")
    assert text.endswith(f"{formatting.DIVIDER}\nЧто дальше?")


# ---------- RPE display ----------


def test_format_set_with_rpe():
    assert formatting.format_set(100.0, 8, 9.0) == "100×8 @9"
    assert formatting.format_set(100.0, 8, 8.5) == "100×8 @8.5"


def test_format_set_without_rpe_unchanged():
    assert formatting.format_set(100.0, 8) == "100×8"
    assert formatting.format_set(100.0, 8, None) == "100×8"


def test_live_session_shows_rpe_only_where_logged():
    block = ExerciseBlockView(
        group_name="грудь", exercise_name="Жим", sets=[(100.0, 8), (100.0, 7)],
        set_rpes=[9.0, None], exercise_id=1,
    )
    lines = formatting.build_live_session_text([block]).splitlines()
    assert "  • 100×8 @9" in lines
    assert "  • 100×7" in lines


def test_workout_summary_prev_line_shows_rpe():
    block = ExerciseBlockView(
        group_name="грудь", exercise_name="Жим", sets=[(100.0, 8)],
        prev_sets=[(97.5, 8), (97.5, 7)], prev_set_rpes=[8.0, None], exercise_id=1,
    )
    text = formatting.build_workout_summary(
        dt.datetime(2026, 7, 17, 10, 0), [block], show_extra_stats=False
    )
    assert "[прошлая: 97.5×8 @8, 97.5×7]" in text


# ---------- format_pr_detail ----------


def test_format_pr_detail_e1rm():
    text = formatting.format_pr_detail("e1rm", 133.3)
    assert text == "🔥 Новый рекорд e1RM: 133.3 кг"


def test_format_pr_detail_reps_at_weight():
    text = formatting.format_pr_detail("reps_at_weight", 8, extra=100.0)
    assert text == "🔥 Новый рекорд повторов: 100 кг × 8"


def test_format_pr_detail_unknown_kind_falls_back():
    assert formatting.format_pr_detail("tonnage", 1000) == "🔥 Новый рекорд"


def test_format_pr_detail_respects_unit():
    text = formatting.format_pr_detail("e1rm", 133.3, unit="lb")
    assert text.endswith("lb")


# ---------- build_exercise_highlights ----------


def test_build_exercise_highlights_groups_and_joins():
    groups = [
        ("Жим лёжа", ["🔥 Новый рекорд e1RM: 133.3 кг"], "↑ e1RM +5.0 кг vs прошлой тренировки этого упражнения"),
        ("Присед", ["🔥 Новый рекорд повторов: 10 на 100 кг"], None),
    ]
    text = formatting.build_exercise_highlights(groups)
    blocks = text.split("\n\n")
    assert len(blocks) == 2
    assert "<b>Жим лёжа</b>" in blocks[0]
    assert "Новый рекорд e1RM" in blocks[0]
    assert "vs прошлой тренировки" in blocks[0]
    assert "<b>Присед</b>" in blocks[1]
    assert "vs прошлой" not in blocks[1]


# ---------- format_comparison_line ----------


def test_format_comparison_line_up():
    assert formatting.format_comparison_line(5.0).startswith("↑")


def test_format_comparison_line_down():
    assert formatting.format_comparison_line(-5.0).startswith("↓")


def test_format_comparison_line_flat():
    assert formatting.format_comparison_line(0.0).startswith("→")


# ---------- format_progress_screen ----------


def _weighted_session(workout_id, started_at, sets):
    return analytics.SessionStats(workout_id, started_at, [analytics.SetRow(w, r) for w, r in sets])


def test_format_progress_screen_no_sessions():
    text = formatting.format_progress_screen("Жим лёжа", [], None, analytics.PersonalRecords())
    assert "Пока нет завершённых тренировок" in text


def test_format_progress_screen_weighted_shows_total_growth():
    sessions = [
        _weighted_session(1, "2026-06-01T10:00:00", [(100.0, 8)]),
        _weighted_session(2, "2026-06-08T10:00:00", [(105.0, 8)]),
    ]
    records = analytics.PersonalRecords(best_e1rm_weight=105.0, best_e1rm_reps=8, max_e1rm=140.0)

    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records)

    assert "<b>Жим лёжа</b>" in text
    assert "e1RM" in text
    assert "e1RM: ↑+6.3 кг с первой тренировки" in text
    assert "/нед" not in text
    assert "vs прошлой тренировки" not in text
    assert "Рекорд: 105×8 · e1RM 140.0 кг" in text


def test_format_progress_screen_single_session_has_no_growth_line():
    sessions = [_weighted_session(1, "2026-06-01T10:00:00", [(100.0, 8)])]
    records = analytics.PersonalRecords(best_e1rm_weight=100.0, best_e1rm_reps=8, max_e1rm=126.7)

    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records)

    assert "с первой тренировки" not in text


def test_format_progress_screen_bodyweight_session():
    sessions = [_weighted_session(1, "2026-06-01T10:00:00", [(0.0, 12), (0.0, 15)])]
    records = analytics.PersonalRecords(max_reps_at_weight={0.0: 15})

    text = formatting.format_progress_screen("Подтягивания", sessions, None, records)

    assert "всего повторов 27" in text
    assert "Рекорд повторов в сете: 15" in text


def test_format_progress_screen_bodyweight_shows_rep_growth():
    sessions = [
        _weighted_session(1, "2026-06-01T10:00:00", [(0.0, 10)]),
        _weighted_session(2, "2026-06-08T10:00:00", [(0.0, 14)]),
    ]
    records = analytics.PersonalRecords(max_reps_at_weight={0.0: 14})

    text = formatting.format_progress_screen("Подтягивания", sessions, None, records)

    assert "Повторы: ↑+4 с первой тренировки" in text


def test_format_progress_screen_respects_limit():
    sessions = [_weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0, 8)]) for i in range(1, 11)]
    records = analytics.PersonalRecords()
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records, limit=2)
    # only the last 2 sessions' dates should be rendered
    assert "09.06.2026" in text
    assert "10.06.2026" in text
    assert "01.06.2026" not in text


def test_format_progress_screen_skips_sessions_without_sets():
    sessions = [
        analytics.SessionStats(1, "2026-06-01T10:00:00", []),
        _weighted_session(2, "2026-06-08T10:00:00", [(100.0, 8)]),
    ]
    records = analytics.PersonalRecords(best_e1rm_weight=100.0, best_e1rm_reps=8, max_e1rm=126.7)
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records)
    assert "01.06.2026" not in text
    assert "08.06.2026" in text


def test_format_progress_screen_newest_session_first():
    sessions = [_weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0, 8)]) for i in range(1, 4)]
    records = analytics.PersonalRecords()
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records)
    assert text.index("03.06.2026") < text.index("02.06.2026") < text.index("01.06.2026")


def test_format_progress_screen_shows_count_when_history_exceeds_limit():
    sessions = [_weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0, 8)]) for i in range(1, 11)]
    records = analytics.PersonalRecords()
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records, limit=2)
    assert "Показано 2 из 10 тренировок" in text


def test_format_progress_screen_no_count_line_when_history_fits():
    sessions = [_weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0, 8)]) for i in range(1, 4)]
    records = analytics.PersonalRecords()
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records, limit=8)
    assert "Показано" not in text


def test_logging_hint_omits_progression_when_disabled():
    from handlers.workout import _logging_hint
    last = [(100.0, 10, None)]
    with_hint = _logging_hint(last, has_sets=True, unit="kg", show_progression=True)
    without = _logging_hint(last, has_sets=True, unit="kg", show_progression=False)
    assert "🎯" in with_hint
    assert "🎯" not in without
    assert "В прошлый раз" in without
