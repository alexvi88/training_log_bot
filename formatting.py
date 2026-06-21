"""Pure text-formatting helpers — build user-facing messages from plain data.

Kept independent of the DB layer so it can be unit tested directly: handlers
are responsible for turning DB rows into the small view dataclasses below.
"""

import datetime as dt
from dataclasses import dataclass
from html import escape
from typing import Literal, Union

from analytics import e1rm

_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

UNIT_LABELS = {"kg": "кг", "lb": "lb"}


def format_weight(weight: float) -> str:
    if weight == int(weight):
        return str(int(weight))
    return f"{weight:.1f}".rstrip("0").rstrip(".")


def format_set(weight: float, reps: int, is_warmup: bool = False) -> str:
    text = f"{format_weight(weight)}×{reps}"
    return f"w{text}" if is_warmup else text


def format_set_slot(slot: tuple[float, int, bool] | None) -> str:
    if slot is None:
        return "—"
    return format_set(*slot)


def format_date_ru(d: dt.datetime) -> str:
    return f"{d.strftime('%d.%m')} ({_WEEKDAYS_RU[d.weekday()]})"


def format_workout_title(started_at: dt.datetime) -> str:
    return f"[Тренировка {started_at.strftime('%d.%m.%Y')}]"


def format_duration(started_at: dt.datetime, finished_at: dt.datetime) -> str:
    total_minutes = max(0, int((finished_at - started_at).total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


@dataclass
class ExerciseBlockView:
    group_name: str
    exercise_name: str
    sets: list[tuple[float, int, bool]]  # weight, reps, is_warmup
    formula: str = "epley"
    type: Literal["single"] = "single"

    @property
    def working_sets(self) -> list[tuple[float, int, bool]]:
        return [s for s in self.sets if not s[2]]

    @property
    def tonnage(self) -> float:
        return sum(w * r for w, r, _ in self.working_sets)

    @property
    def is_bodyweight(self) -> bool:
        ws = self.working_sets
        return bool(ws) and all(w == 0 for w, _, _ in ws)

    @property
    def top_e1rm(self) -> float:
        ws = self.working_sets
        if not ws:
            return 0.0
        return max(e1rm(w, r, self.formula) for w, r, _ in ws)


@dataclass
class SupersetBlockView:
    exercise_names: list[str]
    # per round: per-exercise (weight, reps, is_warmup), or None if that slot wasn't logged
    rounds: list[list[tuple[float, int, bool] | None]]
    type: Literal["superset"] = "superset"

    @property
    def tonnage(self) -> float:
        total = 0.0
        for round_sets in self.rounds:
            for slot in round_sets:
                if slot is not None and not slot[2]:
                    total += slot[0] * slot[1]
        return total

    @property
    def working_set_count(self) -> int:
        return sum(
            1
            for round_sets in self.rounds
            for slot in round_sets
            if slot is not None and not slot[2]
        )


BlockView = Union[ExerciseBlockView, SupersetBlockView]


def _render_single_block(block: ExerciseBlockView, hide_warmups: bool, show_extra: bool) -> list[str]:
    sets = block.working_sets if hide_warmups else block.sets
    lines = [block.exercise_name]
    lines.append("  " + " · ".join(format_set(w, r, warm) for w, r, warm in sets))
    if show_extra and block.working_sets:
        if block.is_bodyweight:
            lines.append(f"  ↳ повторов всего {sum(r for _, r, _ in block.working_sets)}")
        else:
            lines.append(f"  ↳ объём {format_weight(block.tonnage)} · e1RM {block.top_e1rm:.1f}")
    return lines


def _render_superset_block(block: SupersetBlockView, hide_warmups: bool, show_extra: bool) -> list[str]:
    lines = ["🔗 СУПЕРСЕТ", " ⇄ ".join(block.exercise_names)]
    for round_sets in block.rounds:
        if hide_warmups and all(slot is None or slot[2] for slot in round_sets):
            continue
        lines.append("  " + " / ".join(format_set_slot(slot) for slot in round_sets))
    if show_extra:
        lines.append(f"  ↳ объём {format_weight(block.tonnage)}")
    return lines


def build_workout_summary(
    started_at: dt.datetime,
    finished_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    hide_warmups: bool = False,
    show_extra_stats: bool = True,
) -> str:
    lines = [f"🏋️ {format_date_ru(started_at)} · {format_duration(started_at, finished_at)}"]
    if note:
        lines.append(f"📝 {note}")

    last_group: str | None = None
    total_tonnage = 0.0
    exercise_count = 0
    working_set_count = 0

    for block in blocks:
        if isinstance(block, ExerciseBlockView):
            group_label = block.group_name.upper()
            if group_label != last_group:
                lines.append(group_label)
                last_group = group_label
            lines.extend(_render_single_block(block, hide_warmups, show_extra_stats))
            total_tonnage += block.tonnage
            exercise_count += 1
            working_set_count += len(block.working_sets)
        else:
            last_group = None
            lines.extend(_render_superset_block(block, hide_warmups, show_extra_stats))
            total_tonnage += block.tonnage
            exercise_count += len(block.exercise_names)
            working_set_count += block.working_set_count

    lines.append("——————————")
    lines.append(
        f"Σ объём {format_weight(total_tonnage)} · {exercise_count} упражнения · "
        f"{working_set_count} рабочих сетов"
    )
    return "\n".join(lines)


def build_live_session_text(
    blocks: list[BlockView],
    hint: str | None = None,
    hide_warmups: bool = False,
    started_at: dt.datetime | None = None,
) -> str:
    lines = []
    if started_at is not None:
        lines.append(format_workout_title(started_at))
        lines.append("")
    body_lines = []
    for i, block in enumerate(blocks):
        if i > 0:
            body_lines.append("")
        if isinstance(block, ExerciseBlockView):
            sets = block.working_sets if hide_warmups else block.sets
            body_lines.append(f"<b>{escape(block.exercise_name)}</b>")
            body_lines.append("  " + " · ".join(format_set(w, r, warm) for w, r, warm in sets))
        else:
            body_lines.append(" ⇄ ".join(f"<b>{escape(n)}</b>" for n in block.exercise_names))
            for round_sets in block.rounds:
                if hide_warmups and all(slot is None or slot[2] for slot in round_sets):
                    continue
                body_lines.append("  " + " / ".join(format_set_slot(slot) for slot in round_sets))
    lines.extend(body_lines or ["Добавь упражнение, чтобы начать."])
    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


def format_pr_line(
    exercise_name: str, kind: str, value: float, extra: float | None = None, unit: str = "kg"
) -> str:
    u = UNIT_LABELS.get(unit, "кг")
    if kind == "weight":
        return f"🔥 Новый рекорд в {exercise_name}: {format_weight(value)} {u}"
    if kind == "e1rm":
        return f"🔥 Новый рекорд e1RM в {exercise_name}: {value:.1f} {u}"
    if kind == "tonnage":
        return f"🔥 Новый рекорд объёма в {exercise_name}: {format_weight(value)}"
    if kind == "reps_at_weight":
        return f"🔥 Новый рекорд повторов в {exercise_name}: {int(value)} на {format_weight(extra or 0)} {u}"
    return f"🔥 Новый рекорд в {exercise_name}"


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
    lines = [f"📈 {exercise_name}"]
    if not sessions:
        lines.append("Пока нет завершённых тренировок с этим упражнением.")
        return "\n".join(lines)

    is_bw = sessions[-1].is_bodyweight_mode
    for s in sessions[-limit:]:
        d = dt.datetime.fromisoformat(s.started_at)
        top = s.top_set
        if top is None:
            continue
        if is_bw:
            lines.append(
                f"{format_date_ru(d)} · {format_set(top.weight, top.reps)} · "
                f"всего повторов {s.total_reps}"
            )
        else:
            lines.append(
                f"{format_date_ru(d)} · {format_set(top.weight, top.reps)} · e1RM {s.top_e1rm:.1f} · "
                f"объём {format_weight(s.tonnage)} · {len(s.working_sets)} сетов"
            )

    lines.append("")
    if trend is not None:
        arrow = "↑" if trend.direction == "up" else ("↓" if trend.direction == "down" else "→")
        metric = "повторы" if is_bw else "e1RM"
        lines.append(f"Тренд {metric}: {arrow} {trend.slope_per_week:+.2f}/нед")
    if comparison is not None:
        lines.append(format_comparison_line(comparison.e1rm_delta, comparison.tonnage_delta))

    lines.append("")
    if is_bw:
        lines.append(f"Рекорд повторов в сете: {records.max_reps_at_weight and max(records.max_reps_at_weight.values())}")
    else:
        lines.append(
            f"Рекорды: вес {format_weight(records.max_weight)} {u} · e1RM {records.max_e1rm:.1f} {u} · "
            f"объём сессии {format_weight(records.max_session_tonnage)}"
        )
    return "\n".join(lines)


def format_comparison_line(e1rm_delta: float, tonnage_delta: float, unit: str = "kg") -> str:
    u = UNIT_LABELS.get(unit, "кг")
    arrow = "↑" if e1rm_delta > 0 else ("↓" if e1rm_delta < 0 else "→")
    return (
        f"{arrow} e1RM {e1rm_delta:+.1f} {u} · объём {tonnage_delta:+.0f} "
        f"vs прошлой тренировки этого упражнения"
    )
