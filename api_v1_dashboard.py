"""REST `/v1` для главной сводки (домен: сводка) — только чтение.

Тот же экран, что бот показывает картинкой (charts.render_menu_dashboard), но
структурой, а не растром: приложение рисует его нативно. Считается сводка одним
местом на обоих потребителей — dashboard_data.collect, — и здесь остаётся
только перевод её полей в JSON. Вторая реализация того же расчёта разъезжалась
бы с первой молча: сверить картинку с JSON можно только глазами.

Тексты приходят уже локализованными (formatting.menu_*, i18n.t внутри), как и у
достижений, и по той же причине: формулировка живёт в locales/*.json одним
экземпляром, а не ещё раз в старой версии iOS-приложения. Поэтому сбор обёрнут
в i18n.use_lang(user["lang"]) — тем же приёмом, что api_v1_achievements и
фоновые задачи бота. Без обёртки язык ответа был бы тем, который первым дёрнул
модуль в этом процессе (CLAUDE.md, «Ловушка, встретившаяся шесть раз»), и
русскоязычный атлет получил бы английскую сводку.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import dashboard_data
import db
import i18n
from api_v1_common import ApiError, authed_user_id


def _tile_json(tile: tuple) -> dict[str, Any]:
    """Плитка — (подпись, число) или (подпись, число, приписка), см.
    formatting.menu_tiles. Приписки может не быть, и тогда `sub` — null, а не
    пустая строка: клиенту решать по наличию значения, а не по его длине."""
    label, value = tile[0], tile[1]
    sub: Optional[str] = tile[2] if len(tile) > 2 else None
    return {"label": label, "value": value, "sub": sub}


def _dashboard_json(data: dashboard_data.MenuDashboard) -> dict[str, Any]:
    return {
        "headline": data.headline,
        "rank": {"name": data.rank_name, "level": data.rank_level},
        "tiles": [_tile_json(t) for t in data.tiles],
        "volume": {
            "title": data.volume_title,
            "rows": [
                {"group": group, "sets": sets, "status": status}
                for group, sets, status in data.volume_rows
            ],
        },
        "lifts": {
            "title": data.lifts_title,
            "note": data.lifts_note,
            "tiles": [
                {"exercise": exercise, "growth": growth, "detail": detail}
                for exercise, growth, detail in data.lift_tiles
            ],
        },
    }


async def get_dashboard(request: Request) -> JSONResponse:
    """Сводка главного экрана — или `null`, если законченных тренировок ещё нет.

    `null` целиком и HTTP 200, а не 404 и не структура из нулей: пустая сводка —
    не ошибка и не отсутствующий ресурс, а нормальное состояние новичка. Все до
    единого виджета у него пусты, и карточка из нулей сообщала бы только то, что
    она пустая; и бот (handlers.workout._menu_view отдаёт _onboarding()), и
    приложение в этом случае показывают приглашение начать, а не таблицу.

    Числа и статусы отданы как есть (`sets`, `status`, `level`), а текст —
    готовыми строками: у сводки он человеческий (заголовки окон, согласование
    «9 недель подряд»), и собирать его второй раз средствами iOS значило бы
    держать два источника истины — см. те же доводы в api_v1_achievements.
    """
    user_id = await authed_user_id(request)
    user = await db.get_user(user_id)
    if user is None:
        raise ApiError(404, "not_found", "user not found")
    with i18n.use_lang(user["lang"]):
        data = await dashboard_data.collect(user_id)
        if data is None:
            return JSONResponse(None)
        return JSONResponse(_dashboard_json(data))


routes = [
    Route("/dashboard", get_dashboard, methods=["GET"]),
]
