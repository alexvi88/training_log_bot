"""REST `/v1/games*` — мини-игры между подходами (только iOS-приложение).

  POST  /games/results              — партия: {game, score, duration_ms}
  GET   /games                      — лучшие результаты, значки, show_name
  GET   /games/{game}/leaderboard   — таблица лидеров за неделю или за всё время
  PATCH /games/settings             — {show_name}: имя или «Атлет #NNNN»

Правила (игры, потолки счёта, значки, подпись в таблице) — в games.py.

Неправдоподобная партия (отрицательный счёт, короче секунды, выше потолка
games.SCORE_CAPS) отвечает 202 с `accepted: false` и не пишется — без ошибки:
спорить с подделанным запросом незачем, а честному клиенту с багом ошибка на
экране между подходами ничем не поможет.

Частота — в памяти процесса, по атлету, как у остальных предохранителей
(api_v1_diagnostics, api_v1_funnel): RATE_LIMIT_PER_USER партий за
RATE_WINDOW_SECONDS. Партия короче секунды не бывает, так что сотня в час —
это уже не игра между подходами.

Таблица лидеров — без своих аккаунтов (config.limit_preview_ids — тот же
набор, что выкидывает product_metrics из метрик) и без демо-входа App Review
(auth_identities с провайдером review_demo.PROVIDER): их партии — проверки, а
не результаты атлетов. Неделя — с понедельника 00:00 UTC.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Optional

from starlette.requests import Request
from starlette.routing import Route

import config
import db
import games
import timeutil
from api_v1_common import ApiError, JSONResponse, authed_user, json_body

# Тот же, что review_demo.PROVIDER (не импортируем: review_demo тянет за собой
# вход и пароли, а здесь нужно одно имя).
REVIEW_DEMO_PROVIDER = "review_demo"

RATE_WINDOW_SECONDS = 3600
RATE_LIMIT_PER_USER = 120
_hits: dict[int, list[float]] = {}
_HITS_PRUNE_AT = 1024

LEADERBOARD_TOP = 20
PERIODS = ("week", "all")

# Выше — не счёт и не длительность, а мусор: в SQLite INTEGER 64 бита, а JSON
# честно переносит целые только до 2^53.
_MAX_INT = 2**53


def reset_rate_limit() -> None:
    """Для тестов: счётчик живёт в памяти процесса."""
    _hits.clear()


def _allow(user_id: int) -> bool:
    now = time.monotonic()
    start = now - RATE_WINDOW_SECONDS
    hits = [t for t in _hits.get(user_id, []) if t > start]
    if len(hits) >= RATE_LIMIT_PER_USER:
        _hits[user_id] = hits
        return False
    hits.append(now)
    _hits[user_id] = hits
    if len(_hits) > _HITS_PRUNE_AT:
        for key in list(_hits):
            if not any(t > start for t in _hits[key]):
                del _hits[key]
    return True


def _game_or_400(value: Any) -> str:
    if not isinstance(value, str) or value not in games.GAMES:
        raise ApiError(400, "unknown_game", "unknown game")
    return value


def _int_field(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) > _MAX_INT:
        raise ApiError(400, "bad_request", f"field {key} must be int")
    return value


def week_start(now: Optional[dt.datetime] = None) -> str:
    """Понедельник текущей недели, 00:00 UTC — в формате db.now_iso."""
    now = now or timeutil.utc_now()
    monday = now.date() - dt.timedelta(days=now.weekday())
    return dt.datetime.combine(monday, dt.time()).isoformat(timespec="seconds")


async def _ranked(game: str, period: str) -> list[Any]:
    return await db.game_leaderboard_rows(
        game,
        week_start() if period == "week" else None,
        config.limit_preview_ids(),
        REVIEW_DEMO_PROVIDER,
    )


def _rank_of(rows: list[Any], user_id: int) -> Optional[int]:
    for i, row in enumerate(rows, start=1):
        if row["user_id"] == user_id:
            return i
    return None


async def post_result(request: Request) -> JSONResponse:
    user_id, _user = await authed_user(request)
    if not _allow(user_id):
        raise ApiError(429, "rate_limited", "too many game results, try again later")
    body = await json_body(request)
    game = _game_or_400(body.get("game"))
    score = _int_field(body, "score")
    duration_ms = _int_field(body, "duration_ms")
    previous = await db.game_best(user_id, game)
    if not games.is_plausible(game, score, duration_ms):
        return JSONResponse(
            {"best": previous or 0, "is_new_best": False, "week_rank": None, "accepted": False},
            status_code=202,
        )
    await db.add_game_result(user_id, game, score, duration_ms)
    # Первая партия — рекорд, только если в ней что-то есть: «новый рекорд — 0»
    # звучал бы насмешкой.
    is_new_best = score > previous if previous is not None else score > 0
    best = score if previous is None else max(previous, score)
    week_rank = _rank_of(await _ranked(game, "week"), user_id)
    return JSONResponse(
        {"best": best, "is_new_best": is_new_best, "week_rank": week_rank}, status_code=201
    )


def _date_only(value: Optional[str]) -> Optional[str]:
    return value[:10] if value else None


async def get_games(request: Request) -> JSONResponse:
    user_id, _user = await authed_user(request)
    summaries = await db.game_summaries(user_id)
    games_json = []
    for game in games.GAMES:
        s = summaries.get(game)
        games_json.append({
            "game": game,
            "best": s["best"] if s else None,
            "best_at": _date_only(s["best_at"]) if s else None,
            "plays": s["plays"] if s else 0,
        })
    badges_json = []
    for badge in games.BADGES:
        if badge.cumulative:
            reached = games.cumulative_reached_at(
                await db.list_game_scores(user_id, badge.game), badge.threshold
            )
        else:
            reached = await db.first_game_result_at_least(user_id, badge.game, badge.threshold)
        badges_json.append({
            "code": badge.code,
            "title": badge.title,
            # Взятый — констатация, невзятый — цель (см. games.Badge.goal).
            "description": badge.description if reached else badge.goal,
            "earned_at": _date_only(reached),
        })
    return JSONResponse({
        "games": games_json,
        "badges": badges_json,
        "show_name": await db.get_game_show_name(user_id),
    })


async def get_leaderboard(request: Request) -> JSONResponse:
    user_id, _user = await authed_user(request)
    game = _game_or_400(request.path_params["game"])
    period = request.query_params.get("period") or "week"
    if period not in PERIODS:
        raise ApiError(400, "bad_request", "period must be week or all")
    rows = await _ranked(game, period)
    entries = [
        {
            "rank": i,
            "name": games.leaderboard_name(row["user_id"], row["username"], bool(row["show_name"])),
            "score": row["best"],
            "is_me": row["user_id"] == user_id,
        }
        for i, row in enumerate(rows[:LEADERBOARD_TOP], start=1)
    ]
    rank = _rank_of(rows, user_id)
    me = (
        {"rank": rank, "total": len(rows), "best": rows[rank - 1]["best"]}
        if rank is not None else None
    )
    return JSONResponse({"entries": entries, "me": me, "period": period})


async def update_settings(request: Request) -> JSONResponse:
    user_id, _user = await authed_user(request)
    body = await json_body(request)
    show_name = body.get("show_name")
    if not isinstance(show_name, bool):
        raise ApiError(400, "bad_request", "field show_name must be bool")
    await db.set_game_show_name(user_id, show_name)
    return JSONResponse({"show_name": show_name})


routes = [
    Route("/games", get_games, methods=["GET"]),
    Route("/games/results", post_result, methods=["POST"]),
    Route("/games/settings", update_settings, methods=["PATCH"]),
    Route("/games/{game}/leaderboard", get_leaderboard, methods=["GET"]),
]
