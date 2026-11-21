"""REST `/v1` для «Оценки» 0–10 (GET /v1/progress/score) — только чтение.

Формула и статусы — progress_score (там же почему так); здесь только перевод в
JSON, подписи частей и реплика тренера. Тексты — на языке атлета: язык
выставляет authed_user на весь запрос. Имена упражнений — `display_name`
атлета, группы — каталожные, переведённые здесь; e1RM — в единицах атлета
(веса в базе уже лежат в них, users.unit).

`?days=` — длина ряда `history`: 28 (по умолчанию, точка на каждый день), 90
или 365 (точка на каждый седьмой день назад от сегодня — 13 и 53 точек).
Другое значение — 400 с текстом на языке атлета, а не тихая подмена: приложение
шлёт только эти три, и чужое число — ошибка клиента, которую лучше увидеть.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from starlette.requests import Request
from starlette.routing import Route

import formatting
import i18n
import progress_score
import seed_data
import timeutil
from api_v1_common import ApiError, JSONResponse, authed_user


def _r(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def _group_name(group: str) -> str:
    return seed_data.localized_muscle_group_name(group, i18n.get_lang())


def _date_text(day: dt.date, today: dt.date) -> str:
    return formatting.format_day_month_ru(day, today)


def _detail(s: progress_score.DayScore, part: progress_score.Part, today: dt.date) -> str:
    if s.status in ("empty", "paused"):
        return ""
    d = part.detail
    if part.key == "progression":
        if part.points is not None:
            return i18n.t("progress_score.detail.progression", up=d["up"], n=d["n"])
        if s.progression_from is not None:
            return i18n.t("progress_score.detail.progression_from", date=_date_text(s.progression_from, today))
        return i18n.t("progress_score.detail.progression_none")
    if part.key == "regularity":
        return i18n.t("progress_score.detail.regularity", full=d["full"], n=d["n"])
    if part.key == "volume":
        if part.points is None:
            return i18n.t("progress_score.detail.volume_none")
        if d["deload"]:
            return i18n.t("progress_score.detail.volume_deload")
        return i18n.t("progress_score.detail.volume", full=d["full"], n=d["n"])
    if not d["groups"]:
        return ""
    if d["neglected"]:
        return i18n.t("progress_score.detail.balance", neglected=d["neglected"])
    return i18n.t("progress_score.detail.balance_ok")


def coach_line(lever: Optional[progress_score.Lever], unit: str, today: dt.date) -> str:
    if lever is None:
        return ""
    params = dict(lever.params)
    if "group" in params:
        params["group"] = _group_name(params["group"])
    if "date" in params:
        params["date"] = _date_text(params["date"], today)
    if "step" in params:
        params["step"] = f"{params['step']}{formatting.unit_label(unit)}"
    if "score" in params:
        params["score"] = formatting.format_weight(params["score"])
    return i18n.t(f"progress_score.coach.{lever.kind}", **params)


def _history_point(h: progress_score.DayScore) -> dict[str, Any]:
    point: dict[str, Any] = {"date": h.day.isoformat(), "score": h.score}
    for p in h.parts:
        point[p.key] = _r(p.points)
    return point


def report_json(rep: progress_score.ScoreReport, unit: str) -> dict[str, Any]:
    s = rep.today
    today = s.day
    best = rep.best
    calibration = None
    if s.status == "calibrating":
        calibration = {
            "ready_on": s.ready_on.isoformat(),
            "days_done": (today - s.phase.start_day).days,
            "days_total": s.calibration_days,
            "workouts": s.phase.workouts_since_start,
        }
    paused = None
    if s.status == "paused":
        paused = {
            "days": s.pause_days,
            "last_score": s.last_score,
            "last_date": s.last_score_date.isoformat() if s.last_score_date else None,
        }
    return {
        "status": s.status,
        "score": s.score,
        "delta_7d": rep.delta_7d,
        "title": i18n.t("progress_score.title"),
        "coach_line": coach_line(s.lever, unit, today),
        "parts": [
            {
                "key": p.key,
                "points": _r(p.points),
                "max": p.max,
                "label": i18n.t(f"progress_score.part.{p.key}"),
                "detail": _detail(s, p, today),
            }
            for p in s.parts
        ],
        "history": [_history_point(h) for h in rep.history],
        "best": {"date": best.day.isoformat(), "score": best.score} if best else None,
        "period_days": rep.period_days,
        "lifts": [
            {
                "exercise_id": lift.exercise_id,
                "name": lift.name,
                "baseline_e1rm": _r(lift.baseline),
                "current_e1rm": _r(lift.current),
                "change_pct": _r(lift.change_pct),
                "points": _r(lift.points, 2),
                "rated": lift.rated,
            }
            for lift in s.lifts
        ],
        "groups": [
            {
                "name": _group_name(g.group),
                "sets_7d": g.sets_7d,
                "sets_28d": g.sets_28d,
                "points": _r(g.points, 2),
                "neglected": g.neglected,
            }
            for g in s.groups
        ],
        "weeks": [
            {
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "workouts": w.workouts,
                "counted": w.counted,
                "deload": w.deload,
            }
            for w in s.weeks
        ],
        "deload": s.deload,
        "unit": unit,
        "progression_from": s.progression_from.isoformat() if s.progression_from else None,
        "calibration": calibration,
        "paused": paused,
    }


async def get_progress_score(request: Request) -> JSONResponse:
    """Оценка на сегодня (местный день атлета), её разбор и ряд за `?days=`."""
    user_id, user = await authed_user(request)
    raw = request.query_params.get("days")
    period = progress_score.HISTORY_DAYS
    if raw is not None:
        if not raw.isdigit() or int(raw) not in progress_score.PERIODS:
            raise ApiError(400, "bad_score_days", "days must be 28, 90 or 365", key="api.error.bad_score_days")
        period = int(raw)
    today = timeutil.user_today(user)
    rep = await progress_score.collect(user_id, user, today, period)
    return JSONResponse(report_json(rep, user["unit"]))


routes = [
    Route("/progress/score", get_progress_score, methods=["GET"]),
]
