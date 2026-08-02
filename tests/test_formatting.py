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


def test_format_tonnage_never_abbreviates():
    assert formatting.format_tonnage(11000) == "11 тонн"
    assert formatting.format_tonnage(11500) == "11.5 тонны"
    assert formatting.format_tonnage(21000) == "21 тонна"
    assert formatting.format_tonnage(500) == "500кг"


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


def test_build_workout_summary_bodyweight_exercise_hides_total_reps():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [ExerciseBlockView(group_name="пресс", exercise_name="Пресс", sets=[(0.0, 20), (0.0, 15)])]
    text = formatting.build_workout_summary(started, blocks)
    assert "повторов всего" not in text
    assert "e1RM" not in text


def test_build_workout_summary_hides_extra_stats_when_disabled():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [ExerciseBlockView(group_name="грудь", exercise_name="Жим лёжа", sets=[(100.0, 8)])]
    text = formatting.build_workout_summary(started, blocks, show_extra_stats=False)
    assert "e1RM" not in text


def test_build_workout_summary_includes_note():
    started = dt.datetime(2026, 6, 26, 18, 0)
    text = formatting.build_workout_summary(started, [], note="Болело плечо")
    assert "📝 Болело плечо" in text


def test_build_workout_summary_includes_exercise_note():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="спина", exercise_name="Pull down", sets=[(100.0, 8)], note="new training scheme",
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "📝 <i>new training scheme</i>" in text


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


def test_build_workout_summary_always_italicizes_previous_session():
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
    assert "<i>  [прошлая: 95×8]</i>" in text


def test_build_workout_summary_shows_e1rm_delta_and_previous_date():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="грудь",
            exercise_name="Жим лёжа",
            sets=[(100.0, 8)],
            prev_sets=[(95.0, 8)],
            prev_started_at=dt.datetime(2026, 6, 19, 18, 0),
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "vs 19.06" in text
    assert "↑" in text


def test_build_workout_summary_e1rm_line_uses_unit():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [ExerciseBlockView(group_name="грудь", exercise_name="Жим лёжа", sets=[(100.0, 8)])]
    text = formatting.build_workout_summary(started, blocks, unit="lb")
    assert "e1RM" in text
    assert "lb" in text


def test_build_workout_summary_bodyweight_exercise_shows_no_delta_line():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="пресс", exercise_name="Пресс", sets=[(0.0, 20)],
            prev_sets=[(0.0, 15)], prev_started_at=dt.datetime(2026, 6, 19, 18, 0),
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "vs 19.06" not in text
    assert "[прошлая: 0×15]" in text


def test_build_workout_summary_collapses_identical_consecutive_sets():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="спина", exercise_name="Становая", sets=[(190.0, 5), (190.0, 5), (190.0, 5)]
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "190×5 ×3" in text
    assert "190×5, 190×5" not in text


def test_build_workout_summary_does_not_collapse_non_consecutive_matches():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="спина", exercise_name="Тяга", sets=[(100.0, 8), (90.0, 8), (100.0, 8)]
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "×2" not in text
    assert "100×8" in text and "90×8" in text


def test_build_workout_summary_collapses_previous_session_sets_too():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(
            group_name="спина", exercise_name="Становая",
            sets=[(190.0, 5)], prev_sets=[(180.0, 6), (180.0, 6), (180.0, 6)],
        )
    ]
    text = formatting.build_workout_summary(started, blocks)
    assert "[прошлая: 180×6 ×3]" in text


def test_build_workout_summary_max_chars_drops_oldest_exercises():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(group_name="грудь", exercise_name=f"Упражнение {i}", sets=[(100.0, 8)])
        for i in range(20)
    ]
    full_text = formatting.build_workout_summary(started, blocks)
    trimmed = formatting.build_workout_summary(started, blocks, max_chars=400)
    assert formatting.telegram_length(trimmed) <= 400 or "Показано" in trimmed
    assert len(trimmed) < len(full_text)
    assert "Показано" in trimmed


def test_fit_workout_text_shrinks_summary_to_leave_room_for_suffix():
    started = dt.datetime(2026, 6, 26, 18, 0)
    blocks = [
        ExerciseBlockView(group_name="грудь", exercise_name=f"Упражнение {i}", sets=[(100.0, 8)])
        for i in range(30)
    ]

    def build_summary(max_chars):
        return formatting.build_workout_summary(started, blocks, max_chars=max_chars)

    suffix = "x" * 3000
    text = formatting.fit_workout_text(build_summary, suffix, limit=4096)
    assert formatting.telegram_length(text) <= 4096
    assert text.endswith(suffix)


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
    assert "100×8 @9, 100×7" in lines


def test_live_session_active_exercise_keeps_bulleted_sets():
    block = ExerciseBlockView(
        group_name="спина", exercise_name="Тяга", sets=[(50.0, 5), (5.0, 5), (50.0, 5)], exercise_id=2,
    )
    lines = formatting.build_live_session_text([block], active_exercise_id=2).splitlines()
    assert "  • 50×5" in lines
    assert "  • 5×5" in lines


def test_live_session_finished_exercise_sets_on_one_line():
    block = ExerciseBlockView(
        group_name="спина", exercise_name="seated row - cable", sets=[(50.0, 5), (5.0, 5), (50.0, 5)], exercise_id=2,
    )
    lines = formatting.build_live_session_text([block]).splitlines()
    assert "50×5, 5×5, 50×5" in lines


def test_workout_summary_prev_line_shows_rpe():
    block = ExerciseBlockView(
        group_name="грудь", exercise_name="Жим", sets=[(100.0, 8)],
        prev_sets=[(97.5, 8), (97.5, 7)], prev_set_rpes=[8.0, None], exercise_id=1,
    )
    text = formatting.build_workout_summary(
        dt.datetime(2026, 7, 17, 10, 0), [block], show_extra_stats=False
    )
    assert "[прошлая: 97.5×8 @8, 97.5×7]" in text


def test_build_workout_preview_is_compact_no_1rm():
    """The 'repeat this workout?' preview should read like the live tracker's
    already-finished exercises — name plus one comma-joined line of sets — not
    the full workout-summary style with a [GROUP] tag, bulleted sets, and e1RM."""
    blocks = [
        ExerciseBlockView(
            group_name="грудь", exercise_name="Жим лёжа", sets=[(100.0, 8), (100.0, 7)], exercise_id=1,
        ),
        ExerciseBlockView(group_name="спина", exercise_name="Тяга", sets=[], exercise_id=2),
    ]
    text = formatting.build_workout_preview(dt.datetime(2026, 7, 17, 10, 0), blocks)
    lines = text.splitlines()

    assert "<b>Жим лёжа</b>" in lines
    assert "100×8, 100×7" in lines
    assert "[ГРУДЬ]" not in text
    assert "e1RM" not in text
    assert "  •" not in text
    assert "<i>подходов нет</i>" in lines  # exercise with no sets logged


# ---------- format_pr_detail ----------


def test_format_pr_detail_e1rm():
    text = formatting.format_pr_detail("e1rm", 133.3)
    assert text == "🔥 Новый рекорд e1RM: 133.3кг"


def test_format_pr_detail_reps_at_weight():
    text = formatting.format_pr_detail("reps_at_weight", 8, extra=100.0)
    assert text == "🔥 Новый рекорд повторов: 100кг × 8"


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


def test_format_progress_screen_shows_each_sessions_own_note():
    # A note is tied to the specific workout it was written in — it should
    # appear next to that session only, not on every session for the exercise.
    sessions = [
        _weighted_session(1, "2026-06-01T10:00:00", [(100.0, 8)]),
        _weighted_session(2, "2026-06-08T10:00:00", [(105.0, 8)]),
    ]
    records = analytics.PersonalRecords(best_e1rm_weight=105.0, best_e1rm_reps=8, max_e1rm=140.0)

    text = formatting.format_progress_screen(
        "Pull down", sessions, None, records, session_notes={2: "new training scheme"},
    )

    # …, session (newest first), session, e1RM footnote
    blocks = text.split("\n\n")
    newest, oldest = blocks[-3], blocks[-2]
    assert "📝 <i>new training scheme</i>" in newest
    assert "📝" not in oldest


def test_format_progress_screen_weighted_shows_total_growth():
    sessions = [
        _weighted_session(1, "2026-06-01T10:00:00", [(100.0, 8)]),
        _weighted_session(2, "2026-06-08T10:00:00", [(105.0, 8)]),
    ]
    records = analytics.PersonalRecords(best_e1rm_weight=105.0, best_e1rm_reps=8, max_e1rm=140.0)

    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records)

    assert "<b>Жим лёжа</b>" in text
    assert "e1RM" in text
    assert "e1RM: ↑+6.3кг с первой тренировки" in text
    assert "/нед" not in text
    assert "vs прошлой тренировки" not in text
    assert "Рекорд: 105×8 · e1RM 140.0кг" in text


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


def test_format_progress_screen_delta_scopes_to_selected_period_not_all_time():
    """A shorter period must show the same delta the chart itself plots for that
    period (points[-limit:]) — comparing to the very first session ever, no
    matter which period button is selected, would silently disagree with the
    chart's own title and mislabel it "с первой тренировки"."""
    sessions = [
        _weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0 + i, 8)]) for i in range(1, 11)
    ]
    records = analytics.PersonalRecords()

    expected_period_delta = analytics.e1rm(110.0, 8, "epley") - analytics.e1rm(109.0, 8, "epley")
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, records, limit=2)
    assert f"e1RM: ↑+{expected_period_delta:.1f}кг за период" in text
    assert "с первой тренировки" not in text

    # Selecting "Все" (limit covers the whole history) goes back to the
    # from-the-beginning framing, since the window and the full history now match.
    expected_all_time_delta = analytics.e1rm(110.0, 8, "epley") - analytics.e1rm(101.0, 8, "epley")
    text_all = formatting.format_progress_screen("Жим лёжа", sessions, None, records, limit=10)
    assert f"e1RM: ↑+{expected_all_time_delta:.1f}кг с первой тренировки" in text_all


def test_logging_hint_omits_progression_when_disabled():
    from handlers.workout import _logging_hint
    last = [(100.0, 10, None)]
    with_hint = _logging_hint(last, has_sets=True, unit="kg", show_progression=True)
    without = _logging_hint(last, has_sets=True, unit="kg", show_progression=False)
    assert "🎯" in with_hint
    assert "🎯" not in without
    assert "В прошлый раз" in without


def test_logging_hint_puts_goal_on_its_own_line():
    from handlers.workout import _logging_hint
    last = [(100.0, 10, None)]
    hint = _logging_hint(last, has_sets=True, unit="kg", show_progression=True)
    lines = hint.splitlines()
    assert "В прошлый раз" in lines[0] and "Цель" not in lines[0]
    assert lines[0].endswith("100×10")  # no trailing period
    assert "Цель" in lines[1]


def test_progression_hint_says_why_the_weight_went_up():
    """A jumped target looks arbitrary without the reason behind it — the
    commonest complaint about apps that prescribe weights."""
    s = analytics.suggest_progression([(100.0, 10)])
    assert formatting.format_progression_hint(s) == "🎯 Цель: 102.5×9 — взял 10 повторов, добавляем вес"


def test_progression_hint_stays_terse_when_only_a_rep_is_added():
    """This line is redrawn on every logged set, so it earns extra width only
    when the number jumps; "+1 повтор" reads off the "В прошлый раз" line above."""
    s = analytics.suggest_progression([(100.0, 7)])
    assert formatting.format_progression_hint(s) == "🎯 Цель: 100×8"


def test_logging_hint_uses_the_exercises_own_weight_step():
    from handlers.workout import _logging_hint
    last = [(22.0, 10, None)]  # dumbbells, 2kg apart -> 24, not 24.5
    hint = _logging_hint(last, has_sets=True, unit="kg", show_progression=True, inferred_step=2.0)
    assert "🎯 Цель: 24×7" in hint


def test_logging_hint_bumps_heavy_lifts_by_five():
    from handlers.workout import _logging_hint
    hint = _logging_hint([(200.0, 10, None)], has_sets=True, unit="kg", show_progression=True)
    assert "🎯 Цель: 205×9" in hint


def test_logging_hint_shows_achieved_goal_when_today_sets_meet_target():
    from handlers.workout import _logging_hint
    last = [(100.0, 10, None)]  # top of rep range -> next goal is +2.5kg x 9
    not_yet = _logging_hint(last, has_sets=True, unit="kg", show_progression=True, today_sets=[(100.0, 10)])
    achieved = _logging_hint(last, has_sets=True, unit="kg", show_progression=True, today_sets=[(102.5, 9)])
    assert "🎯" in not_yet and "✅" not in not_yet
    assert "✅" in achieved and "Цель выполнена" in achieved
    assert "🎯" not in achieved


# ---------- collapsible folds ----------


def test_telegram_length_ignores_markup_and_counts_emoji_as_two():
    assert formatting.telegram_length("<b>абв</b>") == 3
    assert formatting.telegram_length("🐘") == 2


def test_progress_screen_folds_the_session_list_as_one_block():
    sessions = [_weighted_session(i, f"2026-06-{i:02d}T10:00:00", [(100.0, 8)]) for i in range(1, 9)]
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, analytics.PersonalRecords())

    open_part, sep, folded = text.partition("<blockquote expandable>")
    assert sep, "the session list should fold"
    # The whole list goes in one block — no seam splitting it into shown/hidden.
    assert "2026" not in open_part
    assert "Рекорд" in open_part
    assert folded.count("08.06.2026") == 1
    assert folded.count("01.06.2026") == 1


def test_progress_screen_fits_the_caption_limit():
    # A long history with heavy sets: without trimming this blows past 1024 and
    # ui.safe_edit_photo would delete the screen without putting one back.
    sets = [(100.0 + i, r) for i, r in enumerate(range(12, 4, -1))]
    sessions = [
        _weighted_session(i, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}T10:00:00", sets)
        for i in range(60)
    ]
    text = formatting.format_progress_screen(
        "Разгибание на трицепс на блоке с канатом", sessions, None,
        analytics.PersonalRecords(best_e1rm_weight=107.0, best_e1rm_reps=5, max_e1rm=125.0),
        limit=9999,
    )

    assert formatting.telegram_length(text) <= formatting.CAPTION_LIMIT
    assert "Показано" in text  # and it says so rather than silently dropping history


def test_progress_screen_single_session_is_not_worth_folding():
    sessions = [_weighted_session(1, "2026-06-01T10:00:00", [(100.0, 8)])]
    text = formatting.format_progress_screen("Жим лёжа", sessions, None, analytics.PersonalRecords())
    assert "blockquote" not in text


def test_short_ai_comment_is_left_alone():
    text = formatting.build_ai_comment_block("Хороший прогресс на **жиме**.")
    assert "blockquote" not in text
    assert "<b>жиме</b>" in text


def test_long_ai_comment_folds_under_its_heading():
    comment = "Разбор подхода за подходом. " * 15  # well past FOLD_MIN_CHARS
    heading, sep, folded = formatting.build_ai_comment_block(comment).partition(
        "<blockquote expandable>"
    )
    assert sep
    assert "Комментарий AI-тренера" in heading
    assert "Разбор подхода" in folded


def test_logging_hint_instruction_can_be_hidden():
    from handlers.workout import _logging_hint

    with_instruction = _logging_hint(None, has_sets=False, show_instruction=True)
    without = _logging_hint(None, has_sets=False, show_instruction=False)
    assert "Вес и повторы" in with_instruction
    assert without == ""


def test_logging_hint_hides_instruction_but_keeps_last_session():
    from handlers.workout import _logging_hint

    last = [(100.0, 10, None)]
    hint = _logging_hint(last, has_sets=True, show_progression=False, show_instruction=False)
    assert "Вес и повторы" not in hint
    assert "В прошлый раз" in hint


def test_live_session_text_places_note_under_active_exercise():
    block = ExerciseBlockView(group_name="Ноги", exercise_name="Присед", sets=[(100.0, 5)], exercise_id=1)
    text = formatting.build_live_session_text([block], active_exercise_id=1, note="болит плечо")
    lines = text.splitlines()
    assert "Присед" in lines[0]
    assert "болит плечо" in lines[2]


# ---------- build_history_list ----------


def test_history_list_puts_exercise_names_in_the_body():
    """Real names run 20-30 chars, so two of them already overflow a button
    label — the list carries them in the message text instead."""
    entries = [
        (dt.datetime(2026, 7, 26, 13), ["conventional deadlift", "pull down"], 13),
    ]
    text = formatting.build_history_list(entries)
    assert "26.07.2026 (вс)" in text
    assert "сет" not in text  # set count no longer shown on the date line
    assert "• conventional deadlift" in text
    assert "• pull down" in text


def test_history_list_shows_first_three_names_and_folds_the_rest():
    long_names = [
        "bench press - flat - machine",
        "chest press - horizontal machine",
        "seated row - 1hand - cable",
        "overhead press - machine",
    ]
    entries = [(dt.datetime(2026, 7, 24, 18), long_names, 9)]
    text = formatting.build_history_list(entries)
    assert "• bench press - flat - machine" in text
    assert "• chest press - horizontal machine" in text
    assert "• seated row - 1hand - cable" in text
    assert "+1 другое" in text  # fourth bullet folds the rest
    assert "overhead press" not in text  # folded name isn't spelled out


def test_history_list_keeps_a_full_page_well_inside_the_message_cap():
    entries = [
        (dt.datetime(2026, 7, d, 18), ["conventional deadlift", "abs - pull down block"], 12)
        for d in range(1, 9)
    ]
    text = formatting.build_history_list(entries)
    assert formatting.telegram_length(text) < formatting.MESSAGE_LIMIT


def test_history_list_empty_state():
    assert formatting.build_history_list([]) == "Пока нет завершённых тренировок."
    assert formatting.build_history_list([], empty="ничего") == "ничего"


def test_history_list_marks_a_workout_with_no_exercises():
    entries = [(dt.datetime(2026, 7, 26, 13), [], 0)]
    text = formatting.build_history_list(entries)
    assert "пусто" in text
    assert "сет" not in text  # no set count when there are none


# ---------- build_workout_card (the shareable image's text) ----------


def test_workout_card_collapses_identical_sets_like_the_text_card():
    """The image is what gets posted, and it was still spelling out every set
    long after the message card learned to fold them — which is also what pushed
    its lines past the card's fixed width."""
    blocks = [
        ExerciseBlockView(
            group_name="грудь", exercise_name="chest press - horizontal machine",
            sets=[(83.6, 8)] * 10,
        )
    ]
    _title, body, _footer, _note = formatting.build_workout_card(
        dt.datetime(2026, 7, 26, 13), blocks
    )
    sets_line = body[1]
    assert "83.6×8 ×10" in sets_line
    assert sets_line.count("83.6×8") == 1


def test_workout_card_footer_counts_every_set_not_the_collapsed_ones():
    blocks = [
        ExerciseBlockView(group_name="грудь", exercise_name="Жим", sets=[(100.0, 8)] * 5)
    ]
    _title, _body, footer, _note = formatting.build_workout_card(
        dt.datetime(2026, 7, 26, 13), blocks
    )
    assert "5 сетов" in footer
