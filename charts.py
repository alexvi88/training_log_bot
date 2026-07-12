"""Render progress charts to PNG bytes (matplotlib, Agg backend, in-memory)."""

import datetime as dt
import io
import textwrap

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

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
) -> bytes:
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
        arrow = "↑" if trend.direction == "up" else ("↓" if trend.direction == "down" else "→")
        ax.set_title(f"{title}  {arrow} {trend.slope_per_week:+.2f}/нед")
    else:
        ax.set_title(title)

    ax.set_ylabel(ylabel)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    return _fig_to_png(fig)


# Sequential ramp for the year heatmap: 0 / 1 / 2 / 3+ workouts per day.
# Monotonic in lightness on the dark card background (GitHub dark greens).
HEATMAP_LEVELS = ["#1e242e", "#006d32", "#26a641", "#39d353"]

_MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def render_year_heatmap(day_counts: dict[dt.date, int], today: dt.date, title: str) -> bytes:
    """GitHub-style contribution calendar: 53 week columns x 7 day rows, Monday on top.

    Colour depth encodes workouts per day. Days after `today` in the current
    week are left undrawn, so the grid visibly ends at "now".
    """
    BG = "#12161d"
    FG = "#e6e6e6"
    MUTED = "#9aa4b2"

    this_monday = today - dt.timedelta(days=today.weekday())
    start = this_monday - dt.timedelta(weeks=52)

    fig = plt.figure(figsize=(10.6, 2.4), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.axis("off")
    ax.set_aspect("equal")
    ax.set_xlim(-3.2, 53.4)
    ax.set_ylim(9.2, -3.4)  # inverted so Monday's row sits on top

    for col in range(53):
        monday = start + dt.timedelta(weeks=col)
        for row in range(7):
            day = monday + dt.timedelta(days=row)
            if day > today:
                continue
            level = min(day_counts.get(day, 0), len(HEATMAP_LEVELS) - 1)
            ax.add_patch(
                plt.Rectangle((col + 0.1, row + 0.1), 0.8, 0.8, color=HEATMAP_LEVELS[level], linewidth=0)
            )
        if col > 0 and monday.month != (monday - dt.timedelta(weeks=1)).month:
            ax.text(col + 0.1, -0.7, _MONTHS_RU[monday.month - 1], color=MUTED, fontsize=7, va="center")

    for row, label in ((0, "Пн"), (2, "Ср"), (4, "Пт")):
        ax.text(-0.5, row + 0.55, label, color=MUTED, fontsize=7, ha="right", va="center")

    ax.text(0.1, -2.3, title, color=FG, fontsize=10, fontweight="bold", va="center")

    legend_x = 44
    ax.text(legend_x - 0.4, 8.1, "меньше", color=MUTED, fontsize=7, ha="right", va="center")
    for i, colour in enumerate(HEATMAP_LEVELS):
        ax.add_patch(plt.Rectangle((legend_x + i + 0.1, 7.7), 0.8, 0.8, color=colour, linewidth=0))
    ax.text(legend_x + len(HEATMAP_LEVELS) + 0.5, 8.1, "больше", color=MUTED, fontsize=7, va="center")

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
