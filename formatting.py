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

_TAG_RE = re.compile(r"<[^>]+>")

# A photo caption tops out at 1024 characters where a plain message gets 4096,
# and screens that ride along with a chart (the progress screen) live under the
# smaller cap. Overflowing it isn't a soft failure: ui.safe_edit_photo deletes
# the old screen before re-sending, so a too-long caption leaves the user with
# no screen at all.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# Worth folding only above this much content — see collapsible_if_long. Six lines
# is twice what the collapsed box occupies; the character threshold catches prose,
# which wraps into many screen lines without containing a single newline.
FOLD_MIN_LINES = 6
FOLD_MIN_CHARS = 300

# e1RM is an abbreviation for a concept most people have never met, and the
# number itself gives no hint that it's a calculation rather than a lift that
# happened. The footnote lives on the progress screen and nowhere else: that's
# the screen you open to interpret the metric, so a permanent line there is
# reference material — under the workout card, read after every session, the
# same line would just be something to scroll past.
E1RM_HINT = (
    "ℹ️ <i>e1RM — расчётный максимум в упражнении: какой вес ты смог бы поднять "
    "на один раз (посчитано на основе весов и повторов).</i>"
)


def format_group(name: str) -> str:
    """A muscle group's name as it's shown anywhere in the UI: uppercase.

    The group is context around whatever it labels (an exercise name, a card
    heading), never the thing itself — caps read as a tag at a glance and stop
    the group competing with the name next to it. Applied at render time only:
    the stored name keeps whatever case the user typed.
    """
    return name.upper()


def telegram_length(text: str) -> int:
    """Length as Telegram counts it: markup is parsed into entities and doesn't
    count toward the limit, and characters are measured in UTF-16 code units —
    so an emoji costs two, not one."""
    return len(_TAG_RE.sub("", text).encode("utf-16-le")) // 2


def collapsible_if_long(text: str) -> str:
    """Fold, but only when there is enough to hide.

    A collapsed block is itself a quote box about three lines tall, so folding a
    short list costs the room it saves — and worse, the box draws the eye to the
    part that was judged least important. Below the thresholds the text is
    returned untouched.
    """
    if text.count("\n") + 1 >= FOLD_MIN_LINES or telegram_length(text) >= FOLD_MIN_CHARS:
        return collapsible(text)
    return text


def collapsible(text: str) -> str:
    """Fold a block behind Telegram's expandable blockquote (Bot API 7.4+).

    It renders collapsed to three lines with an expand chevron, and unfolding
    happens entirely on the client — no callback, no edit, no screen re-send —
    which is what makes it cheaper than an inline button for hiding bulk text.
    Clients too old to know the entity draw it as a plain quote, so nothing is
    ever unreachable. Folding does not shorten the message: hidden text still
    counts toward the 4096/1024 limits.

    Callers pass text that is already escaped/marked up: the tags are added
    around it, never to it.
    """
    return f"<blockquote expandable>{text}</blockquote>"


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


_MONTHS_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_day_month_ru(d: dt.date) -> str:
    """"20 июля" — for prose and button labels, where dd.mm.yyyy reads as a form field."""
    return f"{d.day} {_MONTHS_RU_GEN[d.month - 1]}"


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
    prev_started_at: dt.datetime | None = None  # date of the previous session, for the delta line
    note: str | None = None  # exercise's own note (technique cue, injury flag)

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

    @property
    def prev_top_e1rm(self) -> float:
        if not self.prev_sets:
            return 0.0
        return max(e1rm(w, r, self.formula) for w, r in self.prev_sets)

    @property
    def prev_total_reps(self) -> int:
        return sum(r for _, r in self.prev_sets) if self.prev_sets else 0


# A workout is rendered as a flat list of exercise blocks. (Exercises logged in
# parallel — the "superset" entry mechanic — are stored as independent blocks and
# shown the same as any other exercise; there is no separate superset view type.)
BlockView = ExerciseBlockView


def format_date_short(d: dt.datetime) -> str:
    """Compact dd.mm for the inline e1RM-delta annotation — the year and weekday
    only add width there, since the comparison is always to the most recent prior
    session."""
    return d.strftime("%d.%m")


HISTORY_MAX_NAMES = 3


def _history_bullets(names: list[str]) -> list[str]:
    """Up to the first 3 exercise names as bullet lines; any rest are collapsed
    into a single "+N других" bullet rather than spilling the list further."""
    kept = names[:HISTORY_MAX_NAMES]
    lines = [f"• {escape(name)}" for name in kept]
    rest = len(names) - len(kept)
    if rest:
        lines.append(f"• +{rest} {plural_ru(rest, ('другое', 'других', 'других'))}")
    return lines


def build_history_list(
    entries: list[tuple[dt.datetime, list[str], int]],
    header: str = "📚 <b>История тренировок</b>",
    footer: str = "<i>Напиши название упражнения, чтобы найти тренировку с ним.</i>",
    empty: str = "Пока нет завершённых тренировок.",
) -> str:
    """The history list's body: date, then what was in that session.

    The exercise names live here rather than on the buttons because real ones
    ("conventional deadlift", "abs - pull down block") run 20-30 characters —
    two of them already overflow a button label, while the message body has
    thousands of characters going spare.
    """
    if not entries:
        return empty
    lines = [header]
    for started, names, _set_count in entries:
        head = format_date_ru(started)
        lines.append("")
        lines.append(f"<b>{head}</b>")
        if names:
            lines.extend(f"<i>{b}</i>" for b in _history_bullets(names))
        else:
            lines.append("<i>пусто</i>")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def _delta_arrow(delta: float) -> str:
    return "↑" if delta > 0 else ("↓" if delta < 0 else "→")


def format_tonnage(total_kg: float, unit: str = "kg") -> str:
    """Session/lifetime tonnage as a full word ("тонны"/"тонн"), never abbreviated.

    Russian grammar: a non-whole amount (e.g. "1.5 тонны") always takes the
    2-4 form regardless of the leading digit, so only a whole number of tons
    goes through the normal plural_ru rules.
    """
    u = UNIT_LABELS.get(unit, "кг")
    if total_kg >= 1000:
        tons = round(total_kg / 1000, 1)
        tons_str = format_weight(tons)
        forms = ("тонна", "тонны", "тонн")
        word = plural_ru(int(tons), forms) if tons == int(tons) else forms[1]
        return f"{tons_str} {word}"
    return f"{total_kg:.0f}{u}"


def _collapse_formatted_sets(formatted: list[str]) -> list[str]:
    """Merges a run of consecutive, identically-formatted sets into one entry
    with an "×N" suffix — the same notation the parser already accepts on input
    (e.g. "100x8x3"), so a straight run of work sets reads as one line instead
    of N on the finished-workout card.
    """
    collapsed: list[tuple[str, int]] = []
    for s in formatted:
        if collapsed and collapsed[-1][0] == s:
            collapsed[-1] = (s, collapsed[-1][1] + 1)
        else:
            collapsed.append((s, 1))
    return [f"{s} ×{n}" if n > 1 else s for s, n in collapsed]


def _render_single_block(block: ExerciseBlockView, show_extra: bool, unit: str = "kg") -> list[str]:
    u = UNIT_LABELS.get(unit, "кг")
    label = f"{escape(block.exercise_name)} [{format_group(block.group_name)}]"
    lines = [f"<b>{label}</b>"]
    if block.note:
        lines.append(f"  📝 <i>{escape(block.note)}</i>")
    if block.sets:
        formatted = [format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)]
        lines.extend(f"  • {s}" for s in _collapse_formatted_sets(formatted))
    else:
        lines.append("  <i>подходов нет</i>")
    if show_extra and block.sets:
        vs_prev = ""
        if block.prev_sets and block.prev_started_at is not None:
            when = format_date_short(block.prev_started_at)
            if block.is_bodyweight:
                delta = sum(r for _, r in block.sets) - block.prev_total_reps
                vs_prev = f" ({_delta_arrow(delta)}{delta:+d} vs {when})"
            else:
                delta = block.top_e1rm - block.prev_top_e1rm
                vs_prev = f" ({_delta_arrow(delta)}{delta:+.1f}{u} vs {when})"
        if block.is_bodyweight:
            lines.append(f"  ↳ повторов всего {sum(r for _, r in block.sets)}{vs_prev}")
        else:
            lines.append(f"  ↳ e1RM {block.top_e1rm:.1f}{u}{vs_prev}")
    if block.prev_sets:
        formatted_prev = [format_set(w, r, block.prev_rpe_for(i)) for i, (w, r) in enumerate(block.prev_sets)]
        prev_str = ", ".join(_collapse_formatted_sets(formatted_prev))
        lines.append(f"<i>  [прошлая: {prev_str}]</i>")
    return lines


def build_workout_summary(
    started_at: dt.datetime,
    blocks: list[BlockView],
    note: str | None = None,
    show_extra_stats: bool = True,
    duration_seconds: float | None = None,
    unit: str = "kg",
    max_chars: int | None = None,
) -> str:
    """max_chars: if the rendered text would exceed this, oldest exercises are
    dropped from the tail (with a "показано N из M" marker) until it fits —
    see fit_workout_text for why this can't be a simple truncate.
    """
    header = f"<b>{format_date_ru(started_at)}</b>"
    if duration_seconds is not None:
        header += f" · {format_duration(duration_seconds)}"
    head_lines = [header]
    if note:
        head_lines.append(f"📝 {note}")
    head_lines.append("")

    def assemble(keep: list[BlockView]) -> str:
        lines = list(head_lines)
        for i, block in enumerate(keep):
            if i > 0:
                lines.append("")
            lines.extend(_render_single_block(block, show_extra_stats, unit))
        text = "\n".join(lines)
        if len(keep) < len(blocks):
            text += f"\n\n<i>Показано {len(keep)} из {len(blocks)} упражнений — карточка слишком большая.</i>"
        return text

    kept = blocks
    text = assemble(kept)
    while max_chars is not None and len(kept) > 1 and telegram_length(text) > max_chars:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def fit_workout_text(build_summary, suffix: str, limit: int = MESSAGE_LIMIT) -> str:
    """Guards a workout card against Telegram's 4096-char message cap.

    `build_summary` is a callable(max_chars) -> str producing the header+sets
    portion (see build_workout_summary's max_chars). `suffix` is everything
    else already assembled (tonnage, PR highlights, achievements, AI comment)
    — it doesn't depend on the summary, so its length is known up front and
    becomes the summary's budget. This can't be a length estimate on the
    combined text either: ui.safe_edit deletes the old screen before sending
    the new one, so overflowing silently would leave the user with nothing.
    """
    text = build_summary(None)
    full = text + suffix
    if telegram_length(full) <= limit:
        return full
    reserve = telegram_length(full) - telegram_length(text)
    budget = max(limit - reserve - 20, 200)
    return build_summary(budget) + suffix


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
    """Rendered as a card section prefixed by DIVIDER — same convention as highlights.

    The comment itself is folded: it's a paragraph of prose sitting under the
    workout card, where the numbers are what the user came for. Collapsed it
    costs three lines and still reads as an opening sentence, and unfolding is
    a tap on the client (see collapsible).
    """
    return f"{DIVIDER}\n🤖 <b>Комментарий AI-тренера</b>\n{collapsible_if_long(markdown_bold_to_html(comment))}"


# Fun, shareable size comparisons for a tonnage total — (emoji, kg each, declensions),
# light→heavy. Declensions are (1 единица, 2-4 единицы, 5+ единиц), see plural_ru.
_TONNAGE_OBJECTS = [
    ("🐺", 80, ("сенбернар", "сенбернара", "сенбернаров")),
    ("🏍", 200, ("мотоцикл", "мотоцикла", "мотоциклов")),
    ("🐻", 350, ("бурый медведь", "бурых медведя", "бурых медведей")),
    ("🎹", 480, ("рояль", "рояля", "роялей")),
    ("🐴", 550, ("конь", "коня", "коней")),
    ("🐮", 750, ("корова", "коровы", "коров")),
    ("🚗", 1400, ("легковушка", "легковушки", "легковушек")),
    ("🚚", 3500, ("гружёная «Газель»", "гружёные «Газели»", "гружёных «Газелей»")),
    ("🐘", 5000, ("слон", "слона", "слонов")),
    ("🦈", 5500, ("касатка", "касатки", "касаток")),
    ("🚌", 12000, ("автобус", "автобуса", "автобусов")),
]


def format_tonnage_equivalent(total_kg: float, seed: int = 0) -> str | None:
    """A playful "это как N слонов 🐘" comparison clause, without restating the tonnage
    itself — callers fold it into whatever sentence already states the total.

    Picks whichever object gives a believable count (2..40); `seed` (e.g. the
    workout id) rotates the choice so it isn't always the same object. Returns
    None for a tonnage too small to compare (bodyweight-only or very light days).
    """
    if total_kg < 150:
        return None
    candidates = [
        (emoji, forms, round(total_kg / w))
        for emoji, w, forms in _TONNAGE_OBJECTS
        if 2 <= round(total_kg / w) <= 40
    ]
    if not candidates:
        # Above the heaviest bracket (or in a gap): fall back to the biggest object that fits.
        fitting = [
            (emoji, forms, max(1, round(total_kg / w)))
            for emoji, w, forms in _TONNAGE_OBJECTS
            if w <= total_kg
        ]
        if not fitting:
            return None
        candidates = [fitting[-1]]
    emoji, forms, count = candidates[seed % len(candidates)]
    noun = plural_ru(count, forms)
    return f"Это как {count} {noun} {emoji}"


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
        body.append(f"{block.exercise_name} [{format_group(block.group_name)}]")
        if block.sets:
            # Same "190×5 ×3" collapsing the text card uses — a straight run of
            # work sets otherwise spells every one of them out, which on the image
            # is what pushes the line past the card's width.
            formatted = [format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)]
            body.append("  " + ", ".join(_collapse_formatted_sets(formatted)))
        else:
            body.append("  — без подходов")
        exercise_count += 1
        set_count += len(block.sets)
        tonnage += block.tonnage

    ex_word = plural_ru(exercise_count, ("упражнение", "упражнения", "упражнений"))
    set_word = plural_ru(set_count, ("сет", "сета", "сетов"))
    footer = (
        f"{exercise_count} {ex_word} · {set_count} {set_word} · "
        f"{format_weight(tonnage)}{u}"
    )
    return title, body, footer, note


def build_workout_preview(
    started_at: dt.datetime, blocks: list[BlockView], note: str | None = None,
    duration_seconds: float | None = None,
) -> str:
    """Compact preview of a past workout before repeating it: one line per
    exercise plus a single comma-joined line of its sets — the same terse style
    the live tracker uses for already-finished exercises, no e1RM/group tag/prev-set
    clutter, since this is about scanning what was done, not analysing it."""
    header = f"<b>{format_date_ru(started_at)}</b>"
    if duration_seconds is not None:
        header += f" · {format_duration(duration_seconds)}"
    lines = [header]
    if note:
        lines.append(f"📝 {note}")
    lines.append("")
    for block in blocks:
        lines.append(f"<b>{escape(block.exercise_name)}</b>")
        if block.sets:
            lines.append(", ".join(format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)))
        else:
            lines.append("<i>подходов нет</i>")
    return "\n".join(lines)


def build_live_session_text(
    blocks: list[BlockView],
    hint: str | None = None,
    active_exercise_id: int | None = None,
    note: str | None = None,
) -> str:
    body_lines = []
    for i, block in enumerate(blocks):
        if i > 0:
            body_lines.append("")
        is_active = active_exercise_id is not None and block.exercise_id == active_exercise_id
        prefix = "▶ " if is_active else ""
        body_lines.append(f"{prefix}<b>{escape(block.exercise_name)}</b>")
        if is_active:
            body_lines.extend(f"  • {format_set(w, r, block.rpe_for(i))}" for i, (w, r) in enumerate(block.sets))
            if note:
                body_lines.append(f"📝 <i>{escape(note)}</i>")
        elif block.sets:
            body_lines.append(", ".join(format_set(w, r, block.rpe_for(i)) for i, (w, r) in enumerate(block.sets)))
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
        return f"🔥 Новый рекорд e1RM: {value:.1f}{u}"
    if kind == "reps_at_weight":
        return f"🔥 Новый рекорд повторов: {format_weight(extra or 0)}{u} × {int(value)}"
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


def format_new_achievements(new_codes: list[str]) -> str | None:
    """Celebratory block for badges earned right now, shown on the completion card."""
    import achievements

    earned = [achievements.BY_CODE[c] for c in new_codes if c in achievements.BY_CODE]
    if not earned:
        return None
    header = "🏅 <b>Новое достижение!</b>" if len(earned) == 1 else "🏅 <b>Новые достижения!</b>"
    lines = [header] + [f"{a.emoji} <b>{escape(a.title)}</b> — {escape(a.description)}" for a in earned]
    return "\n".join(lines)


def build_achievements_screen(earned: set[str]) -> str:
    """The full 🏅 badge grid: everything unlocked, then everything still locked.

    Both halves always fold on this screen specifically, short lists included:
    it's reached from Progress purely to check a number or brag about a single
    badge, so the header count (11/23) is the answer most taps are actually
    after — the two lists are supporting detail, not the point of the tap.
    """
    import achievements

    got = [a for a in achievements.CATALOG if a.code in earned]
    locked = [a for a in achievements.CATALOG if a.code not in earned]
    lines = [f"🏅 <b>ДОСТИЖЕНИЯ</b> — {len(got)}/{len(achievements.CATALOG)}", ""]
    if got:
        lines.append(
            collapsible(
                "\n".join(f"{a.emoji} <b>{escape(a.title)}</b> — {escape(a.description)}" for a in got)
            )
        )
    if locked:
        lines.append("")
        lines.append(f"<b>Ещё не открыты — {len(locked)}:</b>")
        lines.append(
            collapsible(
                "\n".join(f"🔒 {escape(a.title)} — {escape(a.description)}" for a in locked)
            )
        )
    return "\n".join(lines)


def format_duration_hm(seconds: float) -> str:
    """Compact h/m for the Hall of Fame longest-workout line."""
    minutes = int(seconds // 60)
    h, m = divmod(minutes, 60)
    if h:
        return f"{h} ч {m} мин" if m else f"{h} ч"
    return f"{m} мин"


def _hall_of_fame_lift(name: str, weight: float, reps: int, e1rm_value: float, unit_label: str) -> str:
    """One personal-record line. Bodyweight moves have no load to report, so their
    record is the best set of reps instead of a weight and an e1RM."""
    if weight > 0:
        return f"• {escape(name)} — {format_set(weight, reps)} · e1RM {e1rm_value:.0f}{unit_label}"
    word = plural_ru(reps, ("повтор", "повтора", "повторов"))
    return f"• {escape(name)} — {reps} {word}"


def build_hall_of_fame(
    total_workouts: int,
    tonnage_kg: float,
    tonnage_equivalent: str | None,
    best_week_streak: int,
    longest_workout_seconds: float,
    top_lifts: list[tuple[str, float, int, float]],  # (name, weight, reps, e1rm); weight 0 = bodyweight
    unit: str = "kg",
    max_chars: int | None = None,
) -> str:
    """Lifetime totals plus the user's best lifts, shown above the badge grid
    on the '🏅 Достижения' screen — no heading of its own."""
    u = UNIT_LABELS.get(unit, "кг")
    if total_workouts == 0:
        return "Пока пусто — заверши первую тренировку, и здесь появятся твои рекорды."

    lines = []
    w = plural_ru(total_workouts, ("тренировка", "тренировки", "тренировок"))
    lines.append(f"🗓 Всего тренировок: <b>{total_workouts}</b> {w}")

    if tonnage_kg >= 1000:
        tons = round(tonnage_kg / 1000)
        tonnage_str = f"{tons} {plural_ru(tons, ('тонна', 'тонны', 'тонн'))}"
    else:
        tonnage_str = f"{tonnage_kg:.0f}{u}"
    tonnage_line = f"🏋️ Поднято за всё время: <b>{tonnage_str}</b>"
    if tonnage_equivalent:
        clause = tonnage_equivalent.rstrip(".")
        clause = clause[:1].lower() + clause[1:]
        tonnage_line += f"  ({clause})"
    lines.append(tonnage_line)

    if best_week_streak >= 2:
        wk = plural_ru(best_week_streak, ("неделя", "недели", "недель"))
        lines.append(f"🔥 Лучшая серия: <b>{best_week_streak}</b> {wk} подряд")
    if longest_workout_seconds > 0:
        lines.append(f"⏱ Самая длинная тренировка: <b>{format_duration_hm(longest_workout_seconds)}</b>")

    if not top_lifts:
        return "\n".join(lines)

    entries = [_hall_of_fame_lift(name, weight, reps, e1, u) for name, weight, reps, e1 in top_lifts]
    lines.append("")
    lines.append("<b>Личные рекорды:</b>")
    head = "\n".join(lines)

    # The whole list goes into one fold rather than being split into a visible
    # head and a hidden tail: collapsed, the block already shows its first lines,
    # so a manual split only adds a seam in the middle of one list.
    def assemble(keep: list[str]) -> str:
        text = f"{head}\n{collapsible_if_long(chr(10).join(keep))}"
        if len(keep) < len(entries):
            text += f"\n<i>показано {len(keep)} из {len(entries)}</i>"
        return text

    kept = entries
    text = assemble(kept)
    while max_chars is not None and len(kept) > 1 and telegram_length(text) > max_chars:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def _progress_session_block(session, is_bodyweight: bool, note: str | None = None) -> str:
    """One dated session on the progress screen: date, its sets, the metric,
    and this session's own note, if it had one — a note only ever applies to
    the specific workout it was written in, not the exercise in general."""
    d = dt.datetime.fromisoformat(session.started_at)
    sets_str = ", ".join(format_set(st.weight, st.reps, st.rpe) for st in session.sets)
    tail = (
        f"всего повторов {session.total_reps}"
        if is_bodyweight
        else f"e1RM {session.top_e1rm:.1f}"
    )
    note_line = f"\n📝 <i>{escape(note)}</i>" if note else ""
    return f"<b>{format_date_ru(d)}</b>\n{sets_str}\n{tail}{note_line}"


def format_progress_screen(
    exercise_name: str,
    sessions: list,  # list[analytics.SessionStats], ascending by date
    comparison,  # analytics.ComparisonDelta | None
    records,  # analytics.PersonalRecords
    limit: int = 8,
    unit: str = "kg",
    session_notes: dict[int, str] | None = None,  # {workout_id: note}
) -> str:
    u = UNIT_LABELS.get(unit, "кг")
    lines = [f"📈 <b>{escape(exercise_name)}</b>", ""]
    if not sessions:
        lines.append("Пока нет завершённых тренировок с этим упражнением.")
        return "\n".join(lines)

    is_bw = sessions[-1].is_bodyweight_mode
    window = [s for s in sessions if s.sets]
    candidates = window[-limit:]

    if len(candidates) >= 2:
        # Anchored to the same window the chart plots (points[-limit:]), not the
        # full history — otherwise this delta and the chart's own title delta
        # (which is computed from that same limited window) disagree whenever a
        # shorter period is selected, and "с первой тренировки" would be a lie.
        first, last = candidates[0], candidates[-1]
        since = "с первой тренировки" if len(candidates) == len(window) else "за период"
        if is_bw:
            delta = last.max_reps_in_set - first.max_reps_in_set
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            lines.append(f"Повторы: {arrow}{delta:+d} {since}")
        else:
            delta = last.top_e1rm - first.top_e1rm
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            lines.append(f"e1RM: {arrow}{delta:+.1f}{u} {since}")

    if is_bw:
        best_reps = max(records.max_reps_at_weight.values()) if records.max_reps_at_weight else 0
        lines.append(f"Рекорд повторов в сете: {best_reps}")
    else:
        lines.append(f"Рекорд: {format_set(records.best_e1rm_weight, records.best_e1rm_reps)} · e1RM {records.max_e1rm:.1f}{u}")

    header = "\n".join(lines)
    notes = session_notes or {}
    blocks = [
        _progress_session_block(s, is_bw, notes.get(s.workout_id)) for s in reversed(candidates)
    ]  # newest first

    # A bodyweight exercise's screen is measured in reps and never prints an
    # e1RM, so there's nothing for the footnote to explain there.
    footer = None if is_bw else E1RM_HINT

    def assemble(keep: list[str]) -> str:
        parts = [header, collapsible_if_long("\n\n".join(keep))]
        if len(window) > len(keep):
            n = plural_ru(len(window), ("тренировка", "тренировки", "тренировок"))
            parts.append(f"Показано {len(keep)} из {len(window)} {n}")
        if footer:
            parts.append(footer)
        return "\n\n".join(parts).rstrip()

    # Drop the oldest sessions until the whole thing fits the caption cap. The
    # finished text is what gets measured (folding doesn't shrink it, and the
    # "Показано N из M" tail grows as sessions are dropped), so this can't be a
    # length estimate — it has to be the real string.
    kept = blocks
    text = assemble(kept)
    while len(kept) > 1 and telegram_length(text) > CAPTION_LIMIT:
        kept = kept[:-1]
        text = assemble(kept)
    return text


def build_bodyweight_screen(logs: list, unit: str = "kg", period_logs: list | None = None) -> str:
    """Text for the ⚖️ Вес тела screen: latest value, entry count, and a
    date - weight list for the selected period.

    logs: all rows with `weight` and `logged_at`, ascending by date (as
    db.list_bodyweight_logs returns). period_logs: the subset to list
    (defaults to `logs`) — the caller windows this by the selected period.

    The entry list is trimmed to fit CAPTION_LIMIT: this text is sent as a photo
    caption, and an over-long one doesn't truncate — safe_edit_photo has already
    deleted the previous screen by the time the send fails, so the whole screen
    would vanish from the chat. Same guard, and same reason, as
    format_progress_screen.
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
    n = plural_ru(len(logs), ("запись", "записи", "записей"))
    head = [
        "⚖️ <b>Дневник веса</b>",
        "",
        f"Сейчас: <b>{format_weight(latest_weight)}{u}</b> {format_date_ru(d)}",
        f"Всего {len(logs)} {n}.",
        "",
    ]

    entries = list(reversed(logs if period_logs is None else period_logs))
    rendered = [
        f"{dt.datetime.fromisoformat(r['logged_at']).strftime('%d.%m.%Y')} — "
        f"{format_weight(r['weight'])}{u}"
        for r in entries
    ]

    def assemble(keep: list[str]) -> str:
        lines = list(head) + keep
        if len(keep) < len(rendered):
            lines.append(f"<i>Показано {len(keep)} из {len(rendered)}</i>")
        lines.append("")
        lines.append("Напиши вес, чтобы добавить новую запись.")
        return "\n".join(lines)

    kept = rendered
    text = assemble(kept)
    while len(kept) > 1 and telegram_length(text) > CAPTION_LIMIT:
        kept = kept[:-1]  # oldest first — the recent entries are the interesting ones
        text = assemble(kept)
    return text


def format_progression_hint(suggestion, achieved: bool = False) -> str:
    """"Цель: …" nudge from analytics.suggest_progression, on its own line under
    the "В прошлый раз" line (no bold — the surrounding line is already italicized).
    """
    if suggestion.is_bodyweight:
        goal = f"{suggestion.target_reps} повторов"
    else:
        goal = format_set(suggestion.target_weight, suggestion.target_reps)
    if achieved:
        return f"✅ Цель выполнена: {goal}"
    return f"🎯 Цель: {goal}"


def format_comparison_line(e1rm_delta: float, unit: str = "kg") -> str:
    u = UNIT_LABELS.get(unit, "кг")
    arrow = "↑" if e1rm_delta > 0 else ("↓" if e1rm_delta < 0 else "→")
    return f"{arrow} e1RM {e1rm_delta:+.1f}{u} vs предыдущего рекорда этого упражнения"


# ---------- дневник питания ----------


@dataclass
class FoodItemView:
    """One product within a logged meal, with its own portion and КБЖУ."""
    name: str
    portion: str = ""
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None


@dataclass
class FoodEntryView:
    """One logged meal, as the food-diary screen shows it."""
    id: int
    description: str
    items: list[FoodItemView] | None = None
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    has_photo: bool = False


def format_kcal(value: float | None) -> str:
    return "—" if value is None else f"{round(value):g} ккал"


def _macros_line(protein: float | None, fat: float | None, carbs: float | None, bold: bool = False) -> str:
    """"Б 30 · Ж 12 · У 60 г" — skipped entirely when the model gave no macros.

    bold=True (totals — a meal's or a day's) wraps just the numbers in <b>, so
    they read at a glance without the Б/Ж/У labels competing for weight; the
    per-item breakdown in parentheses stays plain, it's already secondary text.
    """
    parts = [
        (label, v)
        for label, v in (("Б", protein), ("Ж", fat), ("У", carbs))
        if v is not None
    ]
    if not parts:
        return ""
    num = (lambda v: f"<b>{round(v):g}</b>") if bold else (lambda v: f"{round(v):g}")
    return " · ".join(f"{label} {num(v)}" for label, v in parts) + " г"


def _item_line(item: FoodItemView) -> str:
    """"Гранола — 150 г — 630 ккал (Б 15 · Ж 20 · У 90 г)" — portion and macros
    only when the model actually gave them, so a bare guess doesn't show "None"."""
    head = escape(item.name)
    if item.portion:
        head += f" — {escape(item.portion)}"
    head += f" — {format_kcal(item.calories)}"
    macros = _macros_line(item.protein, item.fat, item.carbs)
    return f"{head} ({macros})" if macros else head


def build_food_estimate_text(
    description: str,
    items: list[FoodItemView] | None = None,
    calories: float | None = None,
    protein: float | None = None,
    fat: float | None = None,
    carbs: float | None = None,
    comment: str = "",
    header: str = "🍽 <b>Вот что я вижу:</b>",
) -> str:
    """The confirmation card shown after the model reads a meal, and (with a
    different header) the preview of a correction."""
    lines = [header, "", f"<b>{escape(description or 'Приём пищи')}</b>"]
    if items:
        lines.append("")
        lines.extend(f"• {_item_line(i)}" for i in items)
    lines.append("")
    lines.append(f"Итого: <b>{format_kcal(calories)}</b>")
    macros = _macros_line(protein, fat, carbs, bold=True)
    if macros:
        lines.append(macros)
    if comment:
        lines.append("")
        lines.append(f"<i>{escape(comment)}</i>")
    return "\n".join(lines)


def build_food_day_screen(date: dt.date, entries: list[FoodEntryView]) -> str:
    """One day of the diary: every meal with its per-item КБЖУ, then the day's total."""
    head = f"🍽 <b>Дневник питания — {format_date_ru(dt.datetime.combine(date, dt.time()))}</b>"
    if not entries:
        return (
            f"{head}\n\nЗа этот день пока пусто.\n\n"
            "Напиши, что съел, или пришли фото еды (можно с подписью) — "
            "я прикину калории и БЖУ, а ты подтвердишь."
        )

    lines = [head, ""]
    for i, e in enumerate(entries, start=1):
        photo = " 📷" if e.has_photo else ""
        lines.append(f"<b>{i}. {escape(e.description)}</b>{photo} — {format_kcal(e.calories)}")
        for item in e.items or []:
            lines.append(f"<i>• {_item_line(item)}</i>")
        macros = _macros_line(e.protein, e.fat, e.carbs, bold=True)
        if macros:
            lines.append(f"<i>{macros}</i>")
        lines.append("")

    known = [e.calories for e in entries if e.calories is not None]
    total = sum(known) if known else None
    n = plural_ru(len(entries), ("приём", "приёма", "приёмов"))
    total_line = f"{DIVIDER}\nИтого за день: <b>{format_kcal(total)}</b> · {len(entries)} {n} пищи"
    if known and len(known) < len(entries):
        total_line += f"\n<i>(без калорий: {len(entries) - len(known)})</i>"
    lines.append(total_line)

    day_macros = _macros_line(
        _sum_or_none(e.protein for e in entries),
        _sum_or_none(e.fat for e in entries),
        _sum_or_none(e.carbs for e in entries),
        bold=True,
    )
    if day_macros:
        lines.append(day_macros)
    return "\n".join(lines)


def _sum_or_none(values) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def build_food_history_list(days: list[tuple[dt.date, int, float | None]]) -> str:
    """The history tab: one line per logged day, newest first."""
    if not days:
        return (
            "📚 <b>История питания</b>\n\nПока ничего не записано.\n"
            "Открой день и напиши, что съел."
        )
    lines = ["📚 <b>История питания</b>", ""]
    for date, entries, calories in days:
        n = plural_ru(entries, ("приём", "приёма", "приёмов"))
        lines.append(
            f"<b>{format_date_ru(dt.datetime.combine(date, dt.time()))}</b> — "
            f"{entries} {n} · {format_kcal(calories)}"
        )
    return "\n".join(lines)
