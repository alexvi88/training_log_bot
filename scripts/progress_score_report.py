"""Калибровка «Оценки» (progress_score) на живой базе — только чтение.

Берёт атлетов с законченной тренировкой за последние `--days` дней (свои и
демо-аккаунты — config.limit_preview_ids и входы review_demo — выкидывает),
считает каждому оценку тем же кодом, что GET /v1/progress/score, и печатает:
статусы на сегодня, разброс оценки (децили и гистограмма), средние баллы
частей по всем дням окна и разброс изменения e1RM у оценённых упражнений —
по нему крутится порог «+5% за окно — полный балл» (progress_score.FULL_PCT)
и полоса шума (NOISE_PCT). Только числа, без имён и id.

База открывается `mode=ro` — скрипт ничего не пишет:

    python scripts/progress_score_report.py /data/bot.db --days 28
    fly ssh console -a training-log-bot -C "python scripts/progress_score_report.py /data/bot.db"
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import sqlite3
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db  # noqa: E402
import progress_score  # noqa: E402
import timeutil  # noqa: E402


def _own_ids(conn: sqlite3.Connection) -> set[int]:
    ids = set(config.limit_preview_ids())
    with contextlib.suppress(sqlite3.OperationalError):  # старая база без таблицы
        ids |= {r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM auth_identities WHERE provider = 'review_demo'"
        )}
    return ids


def _report(conn: sqlite3.Connection, user: sqlite3.Row, period: int) -> progress_score.ScoreReport:
    uid = user["telegram_id"]
    tz = timeutil.offset_hours(user)
    formula = user["e1rm_formula"] or "epley"
    today = timeutil.user_today(user)
    day = db._local_day("started_at", tz)
    workout_days = [
        dt.date.fromisoformat(r[0]) for r in conn.execute(
            f"SELECT {day} FROM workouts WHERE user_id = ? AND status = 'finished' ORDER BY {day}", (uid,)
        )
    ]
    start = progress_score.needed_from(workout_days, today, period)
    raw = conn.execute(db.progress_score_sets_sql(tz, formula), (uid, start.isoformat(), today.isoformat()))
    first = conn.execute(db.exercise_first_two_weeks_sql(tz, formula), (uid,))
    return progress_score.report(
        progress_score.rows_from_db(raw.fetchall()), workout_days,
        progress_score.first_weeks_from_db(first.fetchall()), today, user["unit"], period,
    )


def _pct(values: list[float], q: int) -> list[float]:
    return statistics.quantiles(values, n=q) if len(values) >= q else []


def main(path: str, days: int) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    own = _own_ids(conn)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    users = [
        u for u in conn.execute(
            "SELECT * FROM users WHERE telegram_id IN (SELECT DISTINCT user_id FROM workouts "
            "WHERE status = 'finished' AND started_at >= ?)", (since,)
        )
        if u["telegram_id"] not in own
    ]
    period = next(p for p in progress_score.PERIODS if p >= days) if days <= 365 else 365
    statuses: Counter[str] = Counter()
    today_scores: list[float] = []
    parts: dict[str, list[float]] = {k: [] for k in progress_score.PART_KEYS}
    changes: list[float] = []
    for user in users:
        rep = _report(conn, user, period)
        statuses[rep.today.status] += 1
        if rep.today.score is not None:
            today_scores.append(rep.today.score)
        cutoff = rep.today.day - dt.timedelta(days=days)
        for h in rep.history:
            if h.day > cutoff and h.score is not None:
                for p in h.parts:
                    if p.points is not None:
                        parts[p.key].append(p.points)
        changes += [lift.change_pct for lift in rep.today.lifts if lift.rated]
    conn.close()

    print(f"атлетов с тренировкой за {days} дн.: {len(users)}")
    print("статусы сегодня: " + ", ".join(f"{k} {v}" for k, v in statuses.most_common()))
    if today_scores:
        print(f"оценка сегодня: медиана {statistics.median(today_scores):.1f}, "
              f"среднее {statistics.mean(today_scores):.1f}")
        deciles = _pct(today_scores, 10)
        if deciles:
            print("  децили: " + " ".join(f"{x:.1f}" for x in deciles))
        hist = Counter(min(int(s), 9) for s in today_scores)
        for b in range(10):
            n = hist.get(b, 0)
            print(f"  {b:>2}–{b + 1:<2} {n:4}  {'█' * round(40 * n / len(today_scores))}")
    for key, values in parts.items():
        if values:
            print(f"  {key:12} среднее {statistics.mean(values):.2f} из {progress_score.POINTS[key]:.0f}"
                  f"  (дней с частью: {len(values)})")
    if changes:
        noise = sum(1 for c in changes if abs(c) <= progress_score.NOISE_PCT) / len(changes)
        full = sum(1 for c in changes if c >= progress_score.FULL_PCT) / len(changes)
        print(f"изменение e1RM у оценённых упражнений ({len(changes)}): "
              f"медиана {statistics.median(changes):+.1f}%, в шуме ±{progress_score.NOISE_PCT:g}% — {noise:.0%}, "
              f"≥{progress_score.FULL_PCT:g}% — {full:.0%}")
        deciles = _pct(changes, 10)
        if deciles:
            print("  децили: " + " ".join(f"{x:+.1f}" for x in deciles))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path")
    ap.add_argument("--days", type=int, default=28)
    args = ap.parse_args()
    main(args.db_path, args.days)
