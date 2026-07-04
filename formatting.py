"""Pure text-formatting helpers — build user-facing messages from plain data.

Kept independent of the DB layer so it can be unit tested directly: handlers
are responsible for turning DB rows into the small view dataclasses below.
"""

import datetime as dt
from dataclasses import dataclass
from html import escape
from typing import Literal

from analytics import e1rm

_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

UNIT_LABELS = {"kg": "кг", "lb": "lb"}

DIVIDER = "─" * 10


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Russian plural: forms = (1 единица, 2-4 единицы, 5+ единиц)."""
    m = abs(n) % 100
    last = m % 10
    if 11 <= m <= 14:
        return forms[2]
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def format_weight(weight: float) -> str:
    if weight == int(weight):
        return str(int(weight))
    return f"{weight:.1f}".rstrip("0").rstrip(".")


def format_set(weight: float, reps: int) -> str:
    return f"{format_weight(weight)}×{reps}"


def format_date_ru(d: dt.datetime) -> str:
    return f"{d.strftime('%d.%m.%Y')} ({_WEEKDAYS_RU[d.weekday()]})"


@dataclass
class ExerciseBlockView:
    group_name: str
    exercise_name: str
    sets: list[tuple[float, int]]  # weight, reps
    formula: str = "epley"
    type: Literal["single"] = "single"
    exercise_id: int | None = None
    prev_sets: list[tuple[float, int]] | None = None  # sets from the previous session, if any

    @property
    def tonnage(self) -> float:
        return sum(w * r for w, r in self.sets)

    @property
    def is_bodyweight(self) -> bool:
        return bool(self.sets) and all(w == 0 for w, _ in self.sets)

    @property
    def top_e1rm(self) -> float:
        if not self.sets:
            return 0.0
        return max(e1rm(w, r, self.formula) for w, r in self.sets)


# A workout is rendered as a flat list of exercise blocks. (Exercises logged in
# parallel — the "superset" entry mechanic — are stored as independent blocks and
# shown the same as any other exercise; there is no separate superset view type.)
BlockView = ExerciseBlockView


def _render_single_block(block: ExerciseBlockView, show_extra: bool, italic_prev: bool = False) -> list[str]:
    label = f"{escape(block.exercise_name)} [{block.group_name.upper()}]"
    lines = [f"<b>{label}</b>"]
    lines.extend(f"  • {format_set(w, r)}" for w, r in block.sets)
    if show_extra and block.sets:
        if block.is_bodyweight:
            lines.append(f"  ↳ повторов всего {sum(r for _, r in block.sets)}")
        else:
            lines.append(f"  ↳ e1RM {block.top_e1rm:.1f}")
    if block.prev_sets:
        prev_str = ", ".join(format_set(w, r) for w, r in block.prev_sets)
        prev_line = f"  [прошлая: {prev_str}]"
        lines.append(f"<i>{prev_line}</i>" if italic_prev else prev_line)
    return lines


def build_workout_summary(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    show_extra_stats: bool = True,
    italic_prev: bool = False,
) -> str:
    lines = [f"<b>{format_date_ru(started_at)}</b>", ""]
    if note:
        lines.append(f"📝 {note}")

    for i, block in enumerate(blocks):
        if i > 0:
            lines.append("")
        lines.extend(_render_single_block(block, show_extra_stats, italic_prev))

    return "\n".join(lines)


def format_dashboard(dashboard) -> str:
    """One-glance stats block appended under the main-menu greeting.

    Empty string for a brand-new user (nothing to show yet).
    """
    if dashboard.total_workouts == 0:
        return ""
    lines: list[str] = []
    if dashboard.week_streak >= 2:
        weeks = plural_ru(dashboard.week_streak, ("неделю", "недели", "недель"))
        lines.append(f"Серия: {dashboard.week_streak} {weeks} подряд")

    week_word = plural_ru(dashboard.this_week, ("тренировка", "тренировки", "тренировок"))
    lines.append(f"Эта неделя: {dashboard.this_week} {week_word}")

    month_word = plural_ru(dashboard.last_30_days, ("тренировка", "тренировки", "тренировок"))
    lines.append(f"Последние 30 дней: {dashboard.last_30_days} {month_word}")
    return "\n".join(lines)


def build_workout_card(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    unit: str = "kg",
) -> tuple[str, list[str], str, str | None]:
    """Plain-text (no HTML) breakdown of a workout, for rendering to a shareable image.

    Returns (title, body_lines, footer, note) — charts.render_workout_card draws them.
    """
    u = UNIT_LABELS.get(unit, "кг")
    title = format_date_ru(started_at)
    body: list[str] = []
    exercise_count = 0
    set_count = 0
    tonnage = 0.0

    for block in blocks:
        body.append(f"{block.exercise_name} [{block.group_name.upper()}]")
        body.append("  " + ", ".join(format_set(w, r) for w, r in block.sets))
        exercise_count += 1
        set_count += len(block.sets)
        tonnage += block.tonnage

    ex_word = plural_ru(exercise_count, ("упражнение", "упражнения", "упражнений"))
    set_word = plural_ru(set_count, ("сет", "сета", "сетов"))
    footer = (
        f"{exercise_count} {ex_word} · {set_count} {set_word} · "
        f"{format_weight(tonnage)} {u}"
    )
    return title, body, footer, note


def build_live_session_text(
    blocks: list[BlockView],
    hint: str | None = None,
    active_exercise_id: int | None = None,
) -> str:
    body_lines = []
    for i, block in enumerate(blocks):
        if i > 0:
            body_lines.append("")
        prefix = "▶ " if active_exercise_id is not None and block.exercise_id == active_exercise_id else ""
        body_lines.append(f"{prefix}<b>{escape(block.exercise_name)}</b>")
        body_lines.extend(f"  • {format_set(w, r)}" for w, r in block.sets)
    lines = list(body_lines)
    if not lines and not hint:
        lines = ["Добавь упражнение, чтобы начать."]
    if hint:
        if lines:
            lines.append(DIVIDER if body_lines else "")
        lines.append(hint)
    return "\n".join(lines)


def format_pr_detail(kind: str, value: float, extra: float | None = None, unit: str = "kg") -> str:
    """A single PR line, scoped to an exercise that's already named by its surrounding header."""
    u = UNIT_LABELS.get(unit, "кг")
    if kind == "e1rm":
        return f"🔥 Новый рекорд e1RM: {value:.1f} {u}"
    if kind == "reps_at_weight":
        return f"🔥 Новый рекорд повторов: {int(value)} на {format_weight(extra or 0)} {u}"
    return "🔥 Новый рекорд"


def build_exercise_highlights(groups: list[tuple[str, list[str], str | None]]) -> str:
    """Render per-exercise PR/comparison call-outs grouped under each exercise name.

    groups: list of (exercise_name, pr_detail_lines, comparison_line_or_None).
    """
    blocks = []
    for name, pr_lines, comparison in groups:
        lines = [f"<b>{escape(name)}</b>"]
        lines.extend(pr_lines)
        if comparison:
            lines.append(comparison)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_progress_screen(
    exercise_name: str,
    sessions: list,  # list[analytics.SessionStats], ascending by date
    trend,  # analytics.Trend | None
    comparison,  # analytics.ComparisonDelta | None
    records,  # analytics.PersonalRecords
    limit: int = 8,
    unit: str = "kg",
) -> str:
    u = UNIT_LABELS.get(unit, "кг")
    lines = [f"📈 <b>{escape(exercise_name)}</b>", ""]
    if not sessions:
        lines.append("Пока нет завершённых тренировок с этим упражнением.")
        return "\n".join(lines)

    is_bw = sessions[-1].is_bodyweight_mode
    window = [s for s in sessions if s.sets]
    shown = window[-limit:]

    if trend is not None:
        arrow = "↑" if trend.direction == "up" else ("↓" if trend.direction == "down" else "→")
        metric = "повторы" if is_bw else "e1RM"
        lines.append(f"Тренд {metric}: {arrow}{trend.slope_per_week:+.2f}/нед")

    if is_bw:
        lines.append(f"Рекорд повторов в сете: {records.max_reps_at_weight and max(records.max_reps_at_weight.values())}")
    else:
        lines.append(f"Рекорд: {format_set(records.best_e1rm_weight, records.best_e1rm_reps)} · e1RM {records.max_e1rm:.1f} {u}")
    lines.append("")

    for s in reversed(shown):
        d = dt.datetime.fromisoformat(s.started_at)
        sets_str = ", ".join(format_set(st.weight, st.reps) for st in s.sets)
        lines.append(f"<b>{format_date_ru(d)}</b>")
        lines.append(sets_str)
        if is_bw:
            lines.append(f"всего повторов {s.total_reps}")
        else:
            lines.append(f"e1RM {s.top_e1rm:.1f}")
        lines.append("")

    if len(window) > len(shown):
        n = plural_ru(len(window), ("тренировка", "тренировки", "тренировок"))
        lines.append(f"Показано {len(shown)} из {len(window)} {n}")

    return "\n".join(lines).rstrip()


def format_comparison_line(e1rm_delta: float, unit: str = "kg") -> str:
    u = UNIT_LABELS.get(unit, "кг")
    arrow = "↑" if e1rm_delta > 0 else ("↓" if e1rm_delta < 0 else "→")
    return f"{arrow} e1RM {e1rm_delta:+.1f} {u} vs прошлой тренировки этого упражнения"
