"""Render progress charts to PNG bytes (matplotlib, Agg backend, in-memory)."""

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from analytics import linear_trend  # noqa: E402


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
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
        t0 = dates[0]
        xs_days = [(d - t0).total_seconds() / 86400 for d in dates]
        slope_per_day = trend.slope_per_week / 7
        intercept = values[0] - slope_per_day * xs_days[0]
        trend_y = [intercept + slope_per_day * x for x in xs_days]
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


def render_muscle_group_bar(volumes: dict[str, float], title: str = "Объём по группам мышц") -> bytes:
    fig, ax = plt.subplots(figsize=(6, 3.5))
    labels = list(volumes.keys())
    values = list(volumes.values())
    ax.bar(labels, values, color="#3366cc")
    ax.set_ylabel("тоннаж")
    ax.set_title(title)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.grid(True, axis="y", alpha=0.3)
    return _fig_to_png(fig)
