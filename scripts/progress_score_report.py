"""Калибровка оценки прогресса на живой базе — до того, как её увидят атлеты.

Считает ряд оценок (progress_score.score_weeks) у каждого атлета с законченными
тренировками за последние недели и печатает разброс: сколько атлетов с оценкой,
перцентили последней законченной недели, доли по порогам, средние баллы частей
и несколько анонимных рядов (только числа, без имён и id). Свои аккаунты и
аккаунты прогонов/демо не считаются (ai_limits.quota_exempt).

Запуск на машине бота (база — config.DB_PATH):

    fly ssh console -a training-log-bot -C "python scripts/progress_score_report.py --weeks 12"

Цель калибровки — медианный активный атлет около 6, «МАШИНА» (8+) — заметное
меньшинство. Не так — крутить нормы роста и границы в progress_score.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_limits  # noqa: E402
import db  # noqa: E402
import progress_score  # noqa: E402
import timeutil  # noqa: E402


async def main(weeks: int, samples: int) -> None:
    await db.init_db()
    cur = await db.conn().execute(
        "SELECT DISTINCT user_id FROM workouts WHERE status = 'finished' "
        "AND started_at >= date('now', ?)",
        (f"-{weeks * 7} days",),
    )
    user_ids = [r["user_id"] for r in await cur.fetchall()]
    last_scores: list[float] = []
    parts: dict[str, list[float]] = {k: [] for k in progress_score.POINTS}
    series: list[list[float | None]] = []
    no_score = 0
    for uid in user_ids:
        if await ai_limits.quota_exempt(uid):
            continue
        user = await db.get_user(uid)
        if user is None:
            continue
        scored = await progress_score.collect(uid, user, timeutil.user_today(user), weeks)
        finished = [w for w in scored if not w.provisional]
        graded = [w for w in finished if w.score is not None]
        if not graded:
            no_score += 1
            continue
        last = graded[-1]
        last_scores.append(last.score)
        for p in last.parts:
            parts[p.key].append(p.points)
        series.append([w.score for w in finished])
    await db.close_db()

    n = len(last_scores)
    print(f"атлетов с тренировками за {weeks} нед.: {n + no_score}, с оценкой: {n}, без: {no_score}")
    if not n:
        return
    q = statistics.quantiles(last_scores, n=10) if n >= 10 else []
    print(f"последняя неделя: медиана {statistics.median(last_scores):.1f}, среднее {statistics.mean(last_scores):.1f}")
    if q:
        print("децили: " + " ".join(f"{x:.1f}" for x in q))
    tiers = Counter(progress_score.tier_for(s) for s in last_scores)
    for _, key in progress_score.TIERS:
        print(f"  {key:9} {tiers.get(key, 0):4}  {tiers.get(key, 0) / n:5.0%}")
    for key, values in parts.items():
        print(f"  {key:12} среднее {statistics.mean(values):.2f} из {progress_score.POINTS[key]}")
    print("ряды (случайные, анонимно):")
    for row in random.sample(series, min(samples, len(series))):
        print("  " + " ".join("  –" if s is None else f"{s:4.1f}" for s in row))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--samples", type=int, default=15)
    args = ap.parse_args()
    asyncio.run(main(args.weeks, args.samples))
