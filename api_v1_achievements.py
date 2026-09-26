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
бота для фоновых задач (ai_trainer.py, engagement.py):
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
import config
import db
import formatting
import i18n
from api_v1_common import authed_user, query_int


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
    user_id, user = await authed_user(request)
    earned_at = await db.list_achievement_dates(user_id)
    with i18n.use_lang(user["lang"]):
        payload = [_achievement_json(a, earned_at.get(a.code)) for a in achievements.CATALOG]
    return JSONResponse(payload)


def _in_unit(value_kg: float, unit: str) -> float:
    return round(value_kg * config.LB_PER_KG, 1) if unit == "lb" else value_kg


def _nearest_json(bp: "achievements.BadgeProgress", unit: str) -> dict[str, Any]:
    a = achievements.BY_CODE[bp.code]
    is_weight = achievements.FAMILY_BY_CODE[bp.code] in achievements.WEIGHT_FAMILIES
    # Для весовых семейств числа — в единицах атлета, как и остальные веса в
    # /v1 (клиент подписывает их users.unit из /me). Раньше здесь уезжали кг
    # из achievements.nearest_progress, и на экране в фунтах приложение
    # рисовало «100 из 140 · ещё 40» — килограммы без подписи среди фунтов.
    # remaining считаем от уже переведённых чисел, чтобы current + remaining
    # = target сходилось и после округления.
    current = _in_unit(bp.current, unit) if is_weight else bp.current
    target = _in_unit(bp.target, unit) if is_weight else bp.target
    return {
        "code": bp.code,
        "emoji": a.emoji,
        "title": a.title,
        "description": a.description,
        "current": current,
        "target": target,
        "remaining": round(max(target - current, 0.0), 1) if is_weight else bp.remaining,
        # Единица current/target/remaining: "kg"/"lb" у весовых значков, null у
        # счётных (тренировки, недели, упражнения…) — там это штуки.
        "unit": unit if is_weight else None,
        # Готовая фраза бота («ещё 88lb», «осталось 1.5 т», «ещё 3 тренировки»,
        # «4 из 10») на языке атлета — ровно то, что бот пишет в «Ближайших»
        # (formatting.badge_remaining_text). Клиенту незачем собирать её из
        # чисел самому и угадывать семейство и единицу.
        "remaining_text": formatting.badge_remaining_text(bp, unit),
    }


async def nearest_achievements(request: Request) -> JSONResponse:
    """Незаработанные значки, ближайшие к цели, с прогрессом — блок
    «Ближайшие», который на экране бота стоит первым.

    current/target/remaining — числами для полоски прогресса (весовые — в
    единицах атлета, см. `unit`), плюс `remaining_text` — фраза бота целиком
    (formatting.badge_remaining_text): согласование «ещё 15 кг»/«ещё 3
    тренировки» и перевод кг↔lb живут на сервере одни на бота и приложение.
    """
    user_id, user = await authed_user(request)
    limit = query_int(request, "limit", 3, minimum=1, maximum=20)
    earned = await db.list_achievement_codes(user_id)
    ctx = await achievement_sync.aggregate_context(user_id)
    nearest = achievements.nearest_progress(ctx, earned, limit=limit)
    unit = "lb" if user["unit"] == "lb" else "kg"
    with i18n.use_lang(user["lang"]):
        payload = [_nearest_json(bp, unit) for bp in nearest]
    return JSONResponse(payload)


async def achievement_stats(request: Request) -> JSONResponse:
    """Пожизненные экстремумы/счётчики, из которых складываются значки
    (db.achievement_extremes) — отдельно от списка достижений, потому что это
    числа для профиля/статистики, не привязанные к конкретному коду 1:1 (одно
    число может быть порогом сразу нескольких значков одной линейки — см.
    achievements._TIERS_BY_FAMILY)."""
    user_id, user = await authed_user(request)
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
