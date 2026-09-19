"""REST `/v1` для iOS-клиента (домен: достижения) — только чтение.

Транспорт поверх той же логики, что использует бот (achievements.py,
achievement_sync.py), а не вторая реализация: список кодов, «ближайшие» и
экстремумы уже посчитаны там и покрыты тестами (tests/test_achievements*.py,
tests/test_achievement_sync.py) — здесь их незачем пересчитывать заново.
Выдающих эндпоинтов нет и не будет: значок присваивает только серверная
логика по факту тренировки (achievement_sync.evaluate_after_finish/resync),
кнопка в приложении не должна уметь его себе выписать.

Локализация — решение (а): сервер отдаёт код + локализованные title/description,
а не только код. Причины:
  - у достижений (в отличие от чисел дневника) есть человеческий текст, и он
    не поместится в приём "сервер отдаёт числа, клиент рисует текст" — текстов
    под четыре десятка, они меняются и живут в locales/*.json;
  - дублировать их в iOS-приложении — гарантированный рассинхрон: поправили
    формулировку в locales/ru.json — а в старой версии приложения всё ещё
    старый текст, и это два источника истины вместо одного;
  - язык пользователя уже известен серверу без доп. телодвижений — он есть в
    users.lang (то же поле, что уже отдаёт /me, см. api_v1.me).
Рендерим текст через i18n.use_lang(user["lang"]) — тот же приём, что и у
бота для фоновых задач (ai_trainer.py, engagement.py, game_server.py):
контекстная переменная current_lang возвращается к прежнему значению по
выходу из `with`, так что параллельный запрос другого пользователя в этом же
процессе не увидит чужой язык.

achievements.py объясняет, почему title/description у Achievement — это
@property, а не поля: один процесс держит один CATALOG на все языки сразу,
а язык известен только в момент рендера.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import achievement_sync
import achievements
import db
import i18n
from api_v1_common import ApiError, authed_user_id, query_int


def _achievement_json(a: "achievements.Achievement", earned_at: str | None) -> dict[str, Any]:
    return {
        "code": a.code,
        "emoji": a.emoji,
        "title": a.title,
        "description": a.description,
        "earned": earned_at is not None,
        "earned_at": earned_at,
    }


async def list_achievements(request: Request) -> JSONResponse:
    """Весь каталог для текущего пользователя: заработанные вперемешку с ещё
    нет, в порядке CATALOG (том же порядке, что и на экране бота) — сортировку
    "сначала заработанные" оставляем клиенту, у него для этого есть `earned`."""
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    earned_at = await db.list_achievement_dates(user_id)
    with i18n.use_lang(user["lang"]):
        payload = [_achievement_json(a, earned_at.get(a.code)) for a in achievements.CATALOG]
    return JSONResponse(payload)


async def nearest_achievements(request: Request) -> JSONResponse:
    """Незаработанные значки, ближайшие к цели, с прогрессом — блок
    «Ближайшие», который на экране бота стоит первым.

    current/target отдаём числами (как и вес/тоннаж в остальном /v1) — фраза
    вида «ещё 15 кг» требует русского согласования
    (formatting.format_badge_progress), а это уже текст для клиента, а не
    транспорт; из title/description и голых чисел клиент строит свою фразу
    средствами iOS-локализации.
    """
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    limit = query_int(request, "limit", 3, minimum=1, maximum=20)
    earned = await db.list_achievement_codes(user_id)
    ctx = await achievement_sync.aggregate_context(user_id)
    nearest = achievements.nearest_progress(ctx, earned, limit=limit)
    with i18n.use_lang(user["lang"]):
        payload = [
            {
                "code": bp.code,
                "emoji": achievements.BY_CODE[bp.code].emoji,
                "title": achievements.BY_CODE[bp.code].title,
                "description": achievements.BY_CODE[bp.code].description,
                "current": bp.current,
                "target": bp.target,
                "remaining": bp.remaining,
            }
            for bp in nearest
        ]
    return JSONResponse(payload)


async def achievement_stats(request: Request) -> JSONResponse:
    """Пожизненные экстремумы/счётчики, из которых складываются значки
    (db.achievement_extremes) — отдельно от списка достижений, потому что это
    числа для профиля/статистики, не привязанные к конкретному коду 1:1 (одно
    число может быть порогом сразу нескольких значков одной линейки — см.
    achievements._TIERS_BY_FAMILY)."""
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    extremes = await db.achievement_extremes(user_id, tz_offset=int(user["tz_offset"]))
    return JSONResponse(
        {
            "max_sets": extremes["max_sets"],
            "max_tonnage": extremes["max_tonnage"],
            "max_exercises": extremes["max_exercises"],
            "max_bw_reps": extremes["max_bw_reps"],
            "distinct_groups": extremes["distinct_groups"],
            "has_superset": bool(extremes["has_superset"]),
            "early_workouts": extremes["early_workouts"],
        }
    )


routes = [
    Route("/achievements", list_achievements, methods=["GET"]),
    Route("/achievements/nearest", nearest_achievements, methods=["GET"]),
    Route("/achievements/stats", achievement_stats, methods=["GET"]),
]
