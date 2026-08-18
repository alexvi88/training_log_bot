"""Карточка живого трекера: порядок строк и согласие цели с планом."""

import analytics
import formatting
from handlers.workout import _logging_hint

LAST = [(52.5, 6, None), (52.5, 6, None)]


def test_plan_stands_under_last_time_not_above_it():
    """Сверху вниз: что было → что делаем → куда целимся.

    Планом первой строкой человек упирался в цифры программы раньше, чем видел
    свои собственные.
    """
    text = _logging_hint(LAST, has_sets=True, show_instruction=False, target="3×6–12")

    last_at = text.index("Прошлый раз")
    plan_at = text.index("📋 План")
    goal_at = text.index("🎯 Цель")
    assert last_at < plan_at < goal_at


def test_plan_is_shown_even_without_any_history():
    """Истории нет — план всё равно на экране: это всё, что известно про
    сегодняшнее упражнение."""
    text = _logging_hint(None, has_sets=False, show_instruction=False, target="3×6–12")

    assert "📋 План: 3×6–12" in text
    assert "Прошлый раз" not in text


def test_goal_stays_inside_the_planned_rep_range():
    """«План: 3×5–8» над «Цель: 52.5×10» — это спор бота с самим собой.

    Верх диапазона по умолчанию (12 повторов) — догадка на случай, когда схемы
    нет; написанной в программе она обязана уступать.
    """
    text = _logging_hint(
        [(52.5, 8, None)], has_sets=True, show_instruction=False, target="3×5–8"
    )

    # Восемь — верх плана, значит цель уже не «ещё повтор», а прибавка веса.
    assert "🎯 Цель: 55×" in text
    assert "🎯 Цель: 52.5×9" not in text


def test_without_a_plan_the_default_range_still_applies():
    """Схемы нет — подсказка работает как работала."""
    suggestion = analytics.suggest_progression([(52.5, 8)])
    assert suggestion.action == "add_reps"
    assert suggestion.target_reps == 9


def test_an_explicit_program_rule_outranks_the_free_text_scheme():
    """Правило прогрессии структурное и писалось под упражнение, схема — текст."""
    suggestion = analytics.suggest_progression(
        [(52.5, 8)],
        rule={"rule": "double_progression", "reps_top": 12, "step": 2.5},
        planned_reps=(5, 8),
    )
    assert suggestion.action == "add_reps"  # правило разрешает расти до 12
    assert suggestion.target_reps == 9


def test_restart_after_a_weight_bump_respects_the_planned_bottom():
    """После прибавки веса откат по повторам идёт к низу ПЛАНА, а не к 5."""
    suggestion = analytics.suggest_progression([(100.0, 10)], planned_reps=(8, 10))

    assert suggestion.action == "add_weight"
    assert suggestion.target_reps >= 8


def test_planned_rep_range_reads_only_real_ranges():
    assert formatting.planned_rep_range("3×6–12") == (6, 12)
    assert formatting.planned_rep_range("4x6-10") == (6, 10)
    # Одиночное число диапазоном не считаем: расти там некуда.
    assert formatting.planned_rep_range("3×8") is None
    # «3×30–60 сек» — это не про повторы.
    assert formatting.planned_rep_range("3×30–60 сек") is None
    assert formatting.planned_rep_range(None) is None
