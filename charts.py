"""Render progress charts to PNG bytes (matplotlib, Agg backend, in-memory)."""

import datetime as dt
import io
import textwrap

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

from analytics import WEEKLY_VOLUME_MAX, WEEKLY_VOLUME_MIN, linear_trend  # noqa: E402


# Figures are built directly rather than through pyplot: pyplot keeps a single
# global registry of figures per process, and these renders run in worker threads
# (asyncio.to_thread) — two users opening "Прогресс" at the same moment would be
# racing each other for that registry. Going through Figure() also removes the
# need to remember plt.close(), so a forgotten one can't leak.
def _new_figure(**kwargs) -> Figure:
    """A figure with an Agg canvas attached — pyplot normally does this, and
    without it anything that measures text (get_renderer) has no renderer to
    ask."""
    fig = Figure(**kwargs)
    FigureCanvasAgg(fig)
    return fig


def _fig_to_png(fig, dpi: int = 150) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf.read()


def render_metric_over_sessions(
    points: list[tuple[dt.datetime, float]],
    title: str,
    ylabel: str,
    show_weekly_rate: bool = True,
) -> bytes:
    """`show_weekly_rate` picks the title annotation: the per-week trend rate
    (used by the bodyweight diary) or the plain total change across the
    plotted points (used by the exercise progress chart, where a rate reads
    as noise next to "how much did it actually grow")."""
    fig = _new_figure(figsize=(6, 3.5))
    ax = fig.subplots()
    dates = [p[0] for p in points]
    values = [p[1] for p in points]
    ax.plot(dates, values, marker="o", color="#3366cc")

    trend = linear_trend(points)
    if trend is not None and len(points) >= 2:
        t0 = dates[0].date()
        xs_days = [(d.date() - t0).days for d in dates]
        slope_per_day = trend.slope_per_week / 7
        trend_y = [trend.intercept + slope_per_day * x for x in xs_days]
        ax.plot(dates, trend_y, linestyle="--", color="#cc3333", alpha=0.7)
        if show_weekly_rate:
            arrow = "↑" if trend.direction == "up" else ("↓" if trend.direction == "down" else "→")
            ax.set_title(f"{title}  {arrow} {trend.slope_per_week:+.2f}/нед")
        else:
            delta = values[-1] - values[0]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            ax.set_title(f"{title}  {arrow} {delta:+.1f}")
    else:
        ax.set_title(title)

    ax.set_ylabel(ylabel)
    # Ticks only at dates that actually have a point — matplotlib's default date
    # locator picks evenly-spaced calendar dates, which can land on a day with
    # no session and misleadingly imply one happened there.
    max_ticks = 10
    step = max(1, -(-len(dates) // max_ticks))  # ceil division
    ax.set_xticks(dates[::step])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    # Lower dpi than the other charts here: this one gets re-rendered and
    # re-uploaded to Telegram on every period-switch tap, so shaving ~35% off
    # the PNG (barely visible at this chart's small chat-embedded size) trims
    # both the encode and the upload time.
    return _fig_to_png(fig, dpi=100)


# Binary marker for the year heatmap: trained that day, or not. No count-based shading —
# a day essentially never has more than one workout, so a colour ramp would just be noise.
HEATMAP_EMPTY = "#1e242e"
HEATMAP_FILLED = "#4f8cff"  # same accent used elsewhere (e.g. render_workout_card)

_MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def _rounded_cell(ax, x: float, y: float, size: float, colour: str) -> None:
    pad = size * 0.1
    ax.add_patch(
        FancyBboxPatch(
            (x + pad, y + pad), size - 2 * pad, size - 2 * pad,
            boxstyle="round,pad=0,rounding_size=0.14",
            linewidth=0, facecolor=colour,
        )
    )


# Цвета статусов недельного объёма (см. analytics.classify_weekly_volume). Цвет
# здесь — единственный носитель «мало / норма / перебор»: у груди, бицепса и
# трицепса в базе один и тот же эмодзи 💪, так что значком группы их не
# различить, а подпись занята названием.
VOLUME_COLOURS = {
    "low": "#e0a845",
    "in_range": "#45b97c",
    "high": "#e2685a",
}

# Геометрия строки объёма в долях ширины картинки: название группы прижато
# вправо к _VOL_LABEL_RIGHT, дорожка занимает середину, число — у правого края.
_VOL_LABEL_RIGHT = 0.25
_VOL_TRACK_LEFT = 0.275
_VOL_TRACK_RIGHT = 0.945
_VOL_NUMBER_RIGHT = 0.99

# Толщина полосы в точках. Скруглённые концы даёт кап линии, а не
# FancyBboxPatch, как в клетках календаря: у панели объёма оси не
# равномасштабны, и скругление, заданное в данных, растянулось бы в овал.
_VOL_BAR_WIDTH = 7.5


def _volume_scale_max(rows: list[tuple[str, int, str]]) -> int:
    """Правый край шкалы. Коридор всегда виден целиком с запасом, но одна
    ударная неделя (30 подходов на спину) не должна сплющить остальные полосы
    в ничто."""
    busiest = max((sets for _, sets, _ in rows), default=0)
    return max(WEEKLY_VOLUME_MAX + 4, busiest)


def _draw_volume_panel(
    ax, rows: list[tuple[str, int, str]], title: str, bg: str, fg: str, muted: str
) -> None:
    scale = _volume_scale_max(rows)
    span = _VOL_TRACK_RIGHT - _VOL_TRACK_LEFT

    def x_of(sets: int) -> float:
        return _VOL_TRACK_LEFT + span * min(sets, scale) / scale

    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(len(rows) - 0.35, -1.4)  # inverted: самая нагруженная группа сверху

    ax.text(0.04, -0.95, title, color=muted, fontsize=8.5, fontweight="bold", va="center")

    # Целевой коридор — одной подложкой на все строки, а не отдельной пометкой у
    # каждой полосы: норма одна и та же, и рисовать её семь раз значило бы семь
    # раз просить глаз её перечитать.
    lo, hi = x_of(WEEKLY_VOLUME_MIN), x_of(WEEKLY_VOLUME_MAX)
    ax.add_patch(
        Rectangle(
            (lo, -0.5), hi - lo, len(rows),
            linewidth=0, facecolor=HEATMAP_FILLED, alpha=0.17, zorder=0,
        )
    )
    for edge in (lo, hi):
        ax.plot(
            [edge, edge], [-0.5, len(rows) - 0.5],
            color=HEATMAP_FILLED, alpha=0.5, linewidth=0.9,
            linestyle=(0, (1.5, 2)), zorder=1,
        )
    # Подпись коридора — в строке заголовка, а не под последней полосой: снизу она
    # висела в пустоте между панелью и календарём и читалась как подпись к нему.
    ax.text(
        (lo + hi) / 2, -0.95,
        f"НОРМА {WEEKLY_VOLUME_MIN}–{WEEKLY_VOLUME_MAX}",
        color=HEATMAP_FILLED, fontsize=7, ha="center", va="center",
    )

    for row, (label, sets, status) in enumerate(rows):
        ax.text(_VOL_LABEL_RIGHT, row, label, color=muted, fontsize=8, ha="right", va="center")
        ax.plot(
            [_VOL_TRACK_LEFT, _VOL_TRACK_RIGHT], [row, row],
            color=HEATMAP_EMPTY, linewidth=_VOL_BAR_WIDTH,
            solid_capstyle="round", zorder=2,
        )
        # Ноль полосой не рисуется вовсе: полоска нулевой длины со скруглённым
        # капом всё равно выглядит как «немного есть», а здесь ровно наоборот.
        if sets > 0:
            ax.plot(
                [_VOL_TRACK_LEFT, x_of(sets)], [row, row],
                color=VOLUME_COLOURS[status], linewidth=_VOL_BAR_WIDTH,
                solid_capstyle="round", zorder=3,
            )
        ax.text(
            _VOL_NUMBER_RIGHT, row, str(sets),
            color=fg if sets else muted, fontsize=8.5,
            fontweight="bold" if sets else "normal", ha="right", va="center",
        )


def render_year_heatmap(
    day_counts: dict[dt.date, int],
    today: dt.date,
    start: dt.date,
    stat_lines: list[tuple[str, str]],
    volume_rows: list[tuple[str, int, str]] | None = None,
    volume_title: str = "",
) -> bytes:
    """GitHub-style contribution calendar: week columns x 7 day rows, Monday on top.

    `stat_lines` is a list of (label, value) pairs (e.g. "Серия: " / "5 недель
    подряд") rendered as a header above the grid, label in muted ink and value
    bold — this is the dashboard's streak/this-week/30-day summary, drawn into
    the image itself rather than as separate caption text. The grid runs from
    `start` (typically the Monday of the user's first workout, capped at a
    year back) through `today`, so it doesn't waste columns on weeks before
    the user began.

    `volume_rows` — (название группы, подходов, статус) для панели недельного
    объёма, которая рисуется между статистикой и календарём; порядок строк
    задаёт вызывающая сторона (см. formatting.weekly_volume_panel). Пустой
    список — панели нет совсем: семь нулей ничего не сообщают, а место занимают.
    """
    BG = "#12161d"
    FG = "#e6e6e6"
    MUTED = "#9aa4b2"

    start = start - dt.timedelta(days=start.weekday())  # snap to Monday
    columns = (today - start).days // 7 + 1

    rows = list(volume_rows or ())
    stats_h = 0.36 + 0.24 * max(len(stat_lines), 1)
    vol_h = 0.0 if not rows else 0.30 + 0.26 * len(rows)
    grid_w = max(6.6, 2.4 + columns * 0.19)
    grid_h = 2.4
    fig_w, fig_h = grid_w, stats_h + vol_h + grid_h

    fig = _new_figure(figsize=(fig_w, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)

    text_ax = fig.add_axes([0, 1 - stats_h / fig_h, 1, stats_h / fig_h])
    text_ax.set_facecolor(BG)
    text_ax.axis("off")
    text_ax.set_xlim(0, 1)
    text_ax.set_ylim(0, 1)

    row_frac = 1 / (len(stat_lines) + 0.6) if stat_lines else 1
    for i, (label, value) in enumerate(stat_lines):
        y = 1 - (i + 0.85) * row_frac
        label_text = text_ax.text(0.04, y, label, color=MUTED, fontsize=10.5, va="center")
        fig.canvas.draw()
        bbox = label_text.get_window_extent(renderer=fig.canvas.get_renderer())
        bbox_axes = bbox.transformed(text_ax.transAxes.inverted())
        text_ax.text(bbox_axes.x1, y, value, color=FG, fontsize=10.5, fontweight="bold", va="center")

    if rows:
        vol_ax = fig.add_axes([0, grid_h / fig_h, 1, vol_h / fig_h])
        _draw_volume_panel(vol_ax, rows, volume_title, BG, FG, MUTED)

    grid_ax = fig.add_axes([0, 0, 1, grid_h / fig_h])
    grid_ax.set_facecolor(BG)
    grid_ax.axis("off")
    grid_ax.set_aspect("equal")
    grid_ax.set_xlim(-3.2, columns + 0.4)
    grid_ax.set_ylim(9.2, -3.4)  # inverted so Monday's row sits on top

    for col in range(columns):
        monday = start + dt.timedelta(weeks=col)
        for row in range(7):
            day = monday + dt.timedelta(days=row)
            if day > today:
                continue
            colour = HEATMAP_FILLED if day_counts.get(day, 0) > 0 else HEATMAP_EMPTY
            _rounded_cell(grid_ax, col, row, 1, colour)
        if col > 0 and monday.month != (monday - dt.timedelta(weeks=1)).month:
            grid_ax.text(col + 0.1, -0.7, _MONTHS_RU[monday.month - 1], color=MUTED, fontsize=7, va="center")

    for row, label in ((0, "Пн"), (2, "Ср"), (4, "Пт")):
        grid_ax.text(-0.5, row + 0.55, label, color=MUTED, fontsize=7, ha="right", va="center")

    return _fig_to_png(fig)


# At the card's fixed 6.6in width, monospace 12pt fits ~59 characters between the
# margins; wrapping a little short of that leaves room for glyphs wider than the
# measured average.
_CARD_BODY_WIDTH = 52


def _wrap_card_line(line: str) -> list[str]:
    """Wrap one body line to the card's width, keeping a set line's two-space
    indent on its continuations so it stays visually attached to its exercise."""
    if len(line) <= _CARD_BODY_WIDTH:
        return [line]
    indent = "  " if line.startswith("  ") else ""
    return textwrap.wrap(
        line,
        width=_CARD_BODY_WIDTH,
        subsequent_indent=indent + "  ",
        break_long_words=True,
    ) or [line]


def render_workout_card(
    title: str,
    body_lines: list[str],
    footer: str,
    note: str | None = None,
) -> bytes:
    """Render a workout breakdown as a dark, shareable card image.

    Kept emoji-free on purpose: matplotlib's bundled font renders emoji as
    blank boxes, so the card relies on colour and weight for hierarchy instead.
    """
    BG = "#12161d"
    FG = "#e6e6e6"
    ACCENT = "#4f8cff"
    MUTED = "#9aa4b2"
    NOTE = "#d9c98a"

    # (text, style) rows, top to bottom.
    rows: list[tuple[str, str]] = [("ТРЕНИРОВКА", "header"), (title, "muted"), ("", "normal")]
    if note:
        chunks = textwrap.wrap(note, width=46) or [note]
        chunks[0] = "«" + chunks[0]
        chunks[-1] = chunks[-1] + "»"
        for chunk in chunks:
            rows.append((chunk, "note"))
        rows.append(("", "normal"))
    for line in body_lines:
        # exercise headers start at column 0; set lines are indented with two spaces.
        style = "exercise" if line and not line.startswith(" ") else "normal"
        # Wrapped for the same reason the note above is: the figure is a fixed
        # 6.6in wide and nothing clips text, so an over-long line (a long exercise
        # name, or an exercise with many distinct sets) just ran off the right
        # edge of the shared image.
        for chunk in _wrap_card_line(line):
            rows.append((chunk, style))
    rows.append(("─" * 28, "muted"))
    rows.append((footer, "accent"))

    line_h = 0.30
    top_pad, bottom_pad = 0.40, 0.32
    fig_w = 6.6
    fig_h = top_pad + bottom_pad + len(rows) * line_h

    fig = _new_figure(figsize=(fig_w, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, fig_h)

    styles = {
        "header": dict(color=ACCENT, fontsize=17, fontweight="bold"),
        "muted": dict(color=MUTED, fontsize=11),
        "exercise": dict(color=FG, fontsize=12, fontweight="bold"),
        "accent": dict(color=ACCENT, fontsize=12, fontweight="bold"),
        "note": dict(color=NOTE, fontsize=11, style="italic"),
        "normal": dict(color=FG, fontsize=12),
    }

    y = fig_h - top_pad
    for text, style in rows:
        ax.text(0.05, y, text, family="monospace", va="top", **styles[style])
        y -= line_h
    return _fig_to_png(fig)
