"""REST `/v1` «Уведомлений» — раздел в «Профиле» приложения, куда ложится
каждый пуш человеку (inbox.py), даже когда пуши выключены.

- `GET /inbox?before=<id>&limit=<n>` — свежие сверху, страницами по курсору
  `before` (id последней показанной строки; `next_before` в ответе — курсор
  следующей страницы или null, если дальше пусто), плюс `unread` — сколько
  непрочитанных всего (бейдж на «Профиле» и число на иконке).
- `POST /inbox/read` — `{"id": <id>}` отмечает одну строку (тап по карточке,
  тап по пушу с `inbox_id`), `{"all": true}` — все («Прочитать всё»). Чужая,
  несуществующая или уже прочитанная строка — не ошибка, а `marked: 0`:
  строку могла убрать ночная чистка (config.INBOX_RETENTION_DAYS), пока пуш
  лежал в Центре уведомлений.

Отдаются только строки на текущем языке атлета: текст лежит готовым, на
языке пуша (см. схему inbox в db.py), и в ответе /v1 чужого языка не бывает.
`route` — тот же объект, что `route` в APNs payload (push_ios): приложение
ведёт по тапу на карточку туда же, куда по тапу на пуш.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

import api_v1_common as common
import db
import i18n
from api_v1_common import JSONResponse

ApiError = common.ApiError

PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def item_json(row: Any) -> dict[str, Any]:
    route = None
    if row["route"]:
        try:
            route = json.loads(row["route"])
        except ValueError:
            route = None
    return {
        "id": row["id"],
        "category": row["category"],
        "title": row["title"],
        "body": row["body"],
        "route": route if isinstance(route, dict) else None,
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
    }


async def unread_for(user_id: int) -> int:
    """Счётчик непрочитанных на текущем языке — для `/me` и ответов ниже."""
    return await db.count_inbox_unread(user_id, i18n.get_lang())


async def list_inbox(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    limit = common.query_int(request, "limit", PAGE_SIZE, minimum=1, maximum=MAX_PAGE_SIZE)
    before = request.query_params.get("before")
    before_id = common.query_int(request, "before", 0, minimum=1) if before is not None else None
    rows = await db.list_inbox(user_id, i18n.get_lang(), before_id=before_id, limit=limit)
    return JSONResponse(
        {
            "items": [item_json(r) for r in rows],
            "unread": await unread_for(user_id),
            "next_before": rows[-1]["id"] if len(rows) == limit else None,
        }
    )


async def mark_read(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    body = await common.json_body(request)
    item_id = body.get("id")
    if body.get("all") is True:
        marked = await db.mark_inbox_read(user_id)
    elif isinstance(item_id, int) and not isinstance(item_id, bool):
        marked = await db.mark_inbox_read(user_id, item_id=item_id)
    else:
        raise ApiError(400, "bad_request", "send {\"id\": <int>} or {\"all\": true}", key="inbox.error.read_target")
    return JSONResponse({"marked": marked, "unread": await unread_for(user_id)})


routes = [
    Route("/inbox", list_inbox, methods=["GET"]),
    Route("/inbox/read", mark_read, methods=["POST"]),
]
