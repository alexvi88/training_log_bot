"""Render progress charts to PNG bytes (matplotlib, Agg backend, in-memory)."""

import datetime as dt
import io
import textwrap

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from analytics import linear_trend  # noqa: E402


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
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
    fig, ax = plt.subplots(figsize=(6, 3.5))
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
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    return _fig_to_png(fig)


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


def render_year_heatmap(
    day_counts: dict[dt.date, int],
    today: dt.date,
    start: dt.date,
    stat_lines: list[tuple[str, str]],
) -> bytes:
    """GitHub-style contribution calendar: week columns x 7 day rows, Monday on top.

    `stat_lines` is a list of (label, value) pairs (e.g. "Серия: " / "5 недель
    подряд") rendered as a header above the grid, label in muted ink and value
    bold — this is the dashboard's streak/this-week/30-day summary, drawn into
    the image itself rather than as separate caption text. The grid runs from
    `start` (typically the Monday of the user's first workout, capped at a
    year back) through `today`, so it doesn't waste columns on weeks before
    the user began.
    """
    BG = "#12161d"
    FG = "#e6e6e6"
    MUTED = "#9aa4b2"

    start = start - dt.timedelta(days=start.weekday())  # snap to Monday
    columns = (today - start).days // 7 + 1

    stats_h = 0.36 + 0.24 * max(len(stat_lines), 1)
    grid_w = max(6.6, 2.4 + columns * 0.19)
    grid_h = 2.4
    fig_w, fig_h = grid_w, stats_h + grid_h

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150)
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
        rows.append((line, "exercise" if line and not line.startswith(" ") else "normal"))
    rows.append(("─" * 28, "muted"))
    rows.append((footer, "accent"))

    line_h = 0.30
    top_pad, bottom_pad = 0.40, 0.32
    fig_w = 6.6
    fig_h = top_pad + bottom_pad + len(rows) * line_h

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150)
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
