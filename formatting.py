"""Pure text-formatting helpers — build user-facing messages from plain data.

Kept independent of the DB layer so it can be unit tested directly: handlers
are responsible for turning DB rows into the small view dataclasses below.
"""

import datetime as dt
import re
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


def format_rpe(rpe: float | None) -> str:
    """Trailing "@9" / "@8.5" suffix for a set, or empty string when no RPE was logged."""
    if rpe is None:
        return ""
    return f" @{format_weight(rpe)}"


def format_set(weight: float, reps: int, rpe: float | None = None) -> str:
    return f"{format_weight(weight)}×{reps}{format_rpe(rpe)}"


def format_date_ru(d: dt.datetime) -> str:
    return f"{d.strftime('%d.%m.%Y')} ({_WEEKDAYS_RU[d.weekday()]})"


def format_duration(seconds: float) -> str:
    total_minutes = round(seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


@dataclass
class ExerciseBlockView:
    group_name: str
    exercise_name: str
    sets: list[tuple[float, int]]  # weight, reps
    formula: str = "epley"
    type: Literal["single"] = "single"
    exercise_id: int | None = None
    prev_sets: list[tuple[float, int]] | None = None  # sets from the previous session, if any
    set_rpes: list[float | None] | None = None  # per-set RPE, aligned with `sets`; None = none logged
    prev_set_rpes: list[float | None] | None = None  # per-set RPE for prev_sets

    def rpe_for(self, index: int) -> float | None:
        if not self.set_rpes or index >= len(self.set_rpes):
            return None
        return self.set_rpes[index]

    def prev_rpe_for(self, index: int) -> float | None:
        if not self.prev_set_rpes or index >= len(self.prev_set_rpes):
            return None
        return self.prev_set_rpes[index]

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
    if block.sets:
        lines.extend(f"  • {format_set(w, r, block.rpe_for(i))}" for i, (w, r) in enumerate(block.sets))
    else:
        lines.append("  <i>подходов нет</i>")
    if show_extra and block.sets:
        if block.is_bodyweight:
            lines.append(f"  ↳ повторов всего {sum(r for _, r in block.sets)}")
        else:
            lines.append(f"  ↳ e1RM {block.top_e1rm:.1f}")
    if block.prev_sets:
        prev_str = ", ".join(
            format_set(w, r, block.prev_rpe_for(i)) for i, (w, r) in enumerate(block.prev_sets)
        )
        prev_line = f"  [прошлая: {prev_str}]"
        lines.append(f"<i>{prev_line}</i>" if italic_prev else prev_line)
    return lines


def build_workout_summary(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    show_extra_stats: bool = True,
    italic_prev: bool = False,
    duration_seconds: float | None = None,
) -> str:
    header = f"<b>{format_date_ru(started_at)}</b>"
    if duration_seconds is not None:
        header += f" · {format_duration(duration_seconds)}"
    lines = [header]
    if note:
        lines.append(f"📝 {note}")
    lines.append("")

    for i, block in enumerate(blocks):
        if i > 0:
            lines.append("")
        lines.extend(_render_single_block(block, show_extra_stats, italic_prev))

    return "\n".join(lines)


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def markdown_bold_to_html(text: str) -> str:
    """Converts **bold** markers from AI output into Telegram <b> tags.

    The model is asked to wrap exercise names in ** using their exact display
    names; everything else is escaped as plain text, so stray * or HTML-special
    characters elsewhere in the text can't break the message. A ** pair split
    across two chunks (e.g. by Telegram's message-length limit) just falls back
    to literal escaped asterisks in both chunks rather than an unclosed tag.
    """
    parts = []
    pos = 0
    for m in _MD_BOLD_RE.finditer(text):
        parts.append(escape(text[pos : m.start()]))
        parts.append(f"<b>{escape(m.group(1))}</b>")
        pos = m.end()
    parts.append(escape(text[pos:]))
    return "".join(parts)


def format_milestone_line(total_finished: int) -> str:
    """Celebratory one-liner for a round finished-workout count (see analytics.is_workout_milestone)."""
    if total_finished == 1:
        return "🎉 <b>Первая тренировка в дневнике — поехали!</b>"
    word = plural_ru(total_finished, ("тренировка", "тренировки", "тренировок"))
    return f"🎉 <b>Юбилей: {total_finished} {word}!</b> Так держать."


def build_ai_comment_block(comment: str) -> str:
    """Rendered as a card section prefixed by DIVIDER — same convention as highlights."""
    return f"{DIVIDER}\n🤖 <b>Комментарий AI-тренера</b>\n\n{markdown_bold_to_html(comment)}"


# Fun, shareable size comparisons for a session's total tonnage — (emoji+noun, kg each),
# light→heavy. The "N × object" phrasing sidesteps Russian count declension entirely.
_TONNAGE_OBJECTS = [
    ("🐺 сенбернар", 80),
    ("🏍 мотоцикл", 200),
    ("🐻 бурый медведь", 350),
    ("🎹 рояль", 480),
    ("🐴 конь", 550),
    ("🐮 корова", 750),
    ("🚗 легковушка", 1400),
    ("🚚 гружёная «Газель»", 3500),
    ("🐘 слон", 5000),
    ("🦈 касатка", 5500),
    ("🚌 автобус", 12000),
]


def format_tonnage_equivalent(total_kg: float, seed: int = 0) -> str | None:
    """A playful "your session moved N buses" line for the completion card.

    Picks whichever object gives a believable count (2..40); `seed` (e.g. the
    workout id) rotates the choice so it isn't always the same object. Returns
    None for a tonnage too small to compare (bodyweight-only or very light days).
    """
    if total_kg < 150:
        return None
    candidates = [
        (label, round(total_kg / w))
        for label, w in _TONNAGE_OBJECTS
        if 2 <= round(total_kg / w) <= 40
    ]
    if not candidates:
        # Above the heaviest bracket (or in a gap): fall back to the biggest object that fits.
        fitting = [(label, max(1, round(total_kg / w))) for label, w in _TONNAGE_OBJECTS if w <= total_kg]
        if not fitting:
            return None
        candidates = [fitting[-1]]
    label, count = candidates[seed % len(candidates)]
    tonnage = f"{total_kg / 1000:.1f} т" if total_kg >= 1000 else f"{total_kg:.0f} кг"
    return f"🏋️ Суммарно за тренировку — {tonnage}. Это как {count} × {label}."


def dashboard_stat_lines(dashboard) -> list[tuple[str, str]]:
    """(label, value) pairs drawn inside the main-menu heatmap image.

    Empty list for a brand-new user (nothing to show yet).
    """
    if dashboard.total_workouts == 0:
        return []
    lines: list[tuple[str, str]] = []
    if dashboard.week_streak >= 2:
        weeks = plural_ru(dashboard.week_streak, ("неделю", "недели", "недель"))
        lines.append(("Серия: ", f"{dashboard.week_streak} {weeks} подряд"))

    week_word = plural_ru(dashboard.this_week, ("тренировка", "тренировки", "тренировок"))
    lines.append(("Эта неделя: ", f"{dashboard.this_week} {week_word}"))

    month_word = plural_ru(dashboard.last_30_days, ("тренировка", "тренировки", "тренировок"))
    lines.append(("Последние 30 дней: ", f"{dashboard.last_30_days} {month_word}"))
    return lines


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
        if block.sets:
            body.append(
                "  " + ", ".join(format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets))
            )
        else:
            body.append("  — без подходов")
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
        body_lines.extend(f"  • {format_set(w, r, block.rpe_for(i))}" for i, (w, r) in enumerate(block.sets))
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
        return f"🔥 Новый рекорд повторов: {format_weight(extra or 0)} {u} × {int(value)}"
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

    if len(window) >= 2:
        first, last = window[0], window[-1]
        if is_bw:
            delta = last.max_reps_in_set - first.max_reps_in_set
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            lines.append(f"Повторы: {arrow}{delta:+d} с первой тренировки")
        else:
            delta = last.top_e1rm - first.top_e1rm
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            lines.append(f"e1RM: {arrow}{delta:+.1f} {u} с первой тренировки")

    if is_bw:
        best_reps = max(records.max_reps_at_weight.values()) if records.max_reps_at_weight else 0
        lines.append(f"Рекорд повторов в сете: {best_reps}")
    else:
        lines.append(f"Рекорд: {format_set(records.best_e1rm_weight, records.best_e1rm_reps)} · e1RM {records.max_e1rm:.1f} {u}")
    lines.append("")

    for s in reversed(shown):
        d = dt.datetime.fromisoformat(s.started_at)
        sets_str = ", ".join(format_set(st.weight, st.reps, st.rpe) for st in s.sets)
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


def build_bodyweight_screen(logs: list, unit: str = "kg", period_logs: list | None = None) -> str:
    """Text for the ⚖️ Вес тела screen: latest value, entry count, and a
    date - weight list for the selected period.

    logs: all rows with `weight` and `logged_at`, ascending by date (as
    db.list_bodyweight_logs returns). period_logs: the subset to list
    (defaults to `logs`) — the caller windows this by the selected period.
    """
    u = UNIT_LABELS.get(unit, "кг")
    if not logs:
        return (
            "⚖️ <b>Дневник веса</b>\n\nПока нет ни одной записи.\n"
            "Напиши вес — дальше буду показывать динамику."
        )
    latest = logs[-1]
    latest_weight = latest["weight"]
    d = dt.datetime.fromisoformat(latest["logged_at"])
    lines = [
        "⚖️ <b>Дневник веса</b>",
        "",
        f"Сейчас: <b>{format_weight(latest_weight)} {u}</b> {format_date_ru(d)}",
    ]
    n = plural_ru(len(logs), ("запись", "записи", "записей"))
    lines.append(f"Всего {len(logs)} {n}.")
    lines.append("")

    entries = logs if period_logs is None else period_logs
    for r in reversed(entries):
        rd = dt.datetime.fromisoformat(r["logged_at"])
        lines.append(f"{rd.strftime('%d.%m.%Y')} — {format_weight(r['weight'])} {u}")
    lines.append("")

    lines.append("Напиши вес, чтобы добавить новую запись.")
    return "\n".join(lines)


def format_progression_hint(suggestion, unit: str = "kg", achieved: bool = False) -> str:
    """"Цель: …" nudge from analytics.suggest_progression, meant to sit inline
    after the "В прошлый раз" line rather than on its own (no bold — the
    surrounding line is already italicized).
    """
    u = UNIT_LABELS.get(unit, "кг")
    if suggestion.is_bodyweight:
        goal = f"{suggestion.target_reps} повторов (на один больше прошлого)"
    elif suggestion.action == "add_weight":
        top = suggestion.target_reps
        goal = f"пора добавить вес — {format_weight(suggestion.target_weight)} {u} × {top}-{top + 1}"
    else:
        goal = f"{format_set(suggestion.target_weight, suggestion.target_reps)} (тот же вес, +1 повтор)"
    if achieved:
        return f"✅ Цель выполнена: {goal}"
    return f"🎯 Цель: {goal}"


def format_comparison_line(e1rm_delta: float, unit: str = "kg") -> str:
    u = UNIT_LABELS.get(unit, "кг")
    arrow = "↑" if e1rm_delta > 0 else ("↓" if e1rm_delta < 0 else "→")
    return f"{arrow} e1RM {e1rm_delta:+.1f} {u} vs предыдущего рекорда этого упражнения"
