"""REST `/v1` переписки с поддержкой — двусторонний канал между атлетом и
владельцем (`config.ADMIN_ID`).

До этого модуля был только `POST /feedback` (api_v1_feedback.py): текст и фото
улетали админу в Telegram, и на этом всё — ответить человеку было некуда, у
app-only аккаунта (вошёл через Apple) нет чата с ботом. Теперь каждая реплика
лежит в `support_messages` одной веткой на атлета:

- **атлет** читает свою ветку (`GET /support/messages`), пишет в неё (`POST
  /support/messages` — ровно тот же приём, проверки, квота и коды ошибок, что
  у `/feedback`, общий код в api_v1_feedback.read_incoming/store_and_forward)
  и отмечает ответы прочитанными (`POST /support/read`);
- **админ** — тот же аккаунт, что `config.ADMIN_ID`, отдельной роли нет —
  видит список веток (`GET /support/threads`), читает и отвечает в любую
  (`/support/threads/{user_id}/...`). Остальным эти маршруты отвечают 403
  `forbidden`;
- ответить можно и из Telegram: реплаем на пересланное админу сообщение
  (handlers/admin.py, `support_reply`) — ответ ложится в ту же ветку через
  `send_admin_reply`.

Ответ поддержки доходит до атлета только пушем (APNs) и в самой ветке: в
Telegram его не дублируем — у атлета приложения Telegram может не быть вовсе,
а у того, кто есть, отзыв из приложения и ответ в боте разъехались бы по двум
местам.

Форма реплики — одна на обе стороны (`message_json`): `from` — `"user"` или
`"support"` (так клиент рисует пузырь, внутреннее `admin` наружу не
выносим), `photo_url` — путь `/support/photos/{id}` относительно `/v1` (тот же
приём, что `image_url` у истории чата тренера), байты — только владельцу
ветки и админу.

`user_id` в пути — строкой с разбором вручную, а не `{user_id:int}`: у
app-only аккаунта id отрицательный (db.create_app_only_user), а конвертер
Starlette `int` минуса не пускает — такая ветка отвечала бы 404.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

import api_v1_common as common
import api_v1_feedback
import apns
import chat_attachments
import config
import db
import i18n
import push_ios

logger = logging.getLogger(__name__)

ApiError = common.ApiError

# Ответ поддержки — тот же потолок, что у реплики атлета (сообщение Telegram).
MAX_REPLY_LENGTH = api_v1_feedback.MAX_FEEDBACK_LENGTH

# Сколько символов ответа влезает в тело банера атлету после «Ответ
# поддержки: » — с запасом под push_ios.BODY_LIMIT на обоих языках.
_PUSH_TEXT_CLIP = 80

_PHOTO_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def is_support_admin(user_id: int) -> bool:
    return config.ADMIN_ID is not None and user_id == config.ADMIN_ID


def message_json(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "from": "user" if row["sender"] == "user" else "support",
        "text": row["text"],
        "photo_url": f"/support/photos/{row['id']}" if row["photo_path"] else None,
        "created_at": row["created_at"],
    }


async def unread_for(user_id: int) -> int:
    """Счётчик для `/me`: админу — все непрочитанные реплики атлетов, атлету —
    непрочитанные ответы поддержки."""
    if is_support_admin(user_id):
        return await db.support_unread_for_admin()
    return await db.support_unread_for_user(user_id)


async def _push_user_reply(user_id: int, text: str) -> int:
    """Банер атлету «Ответ поддержки: …» на его языке, на каждое устройство.
    Возвращает, сколько устройств Apple принял (0 — APNs не настроен или
    токенов нет). Не бросает: ответ уже записан в ветку."""
    if not apns.is_configured():
        return 0
    try:
        tokens = await db.get_push_tokens(user_id, "ios")
        if not tokens:
            return 0
        user = await db.get_user(user_id)
        lang = user["lang"] if user is not None else i18n.DEFAULT_LANG
        title = i18n.t_in(lang, "support.push.reply_title")
        body = i18n.t_in(
            lang, "support.push.reply_body", text=push_ios._clip_param(text, _PUSH_TEXT_CLIP)
        )
        accepted = 0
        for token in tokens:
            if await apns.send_alert(
                user_id, token, title, body, category="support", route=push_ios.support_route()
            ):
                accepted += 1
        return accepted
    except Exception:
        logger.exception("support: reply push failed for user %s", user_id)
        return 0


async def send_admin_reply(user_id: int, text: str) -> tuple[Any, int]:
    """Ответ поддержки в ветку `user_id` + пуш атлету. Общий для `POST
    /support/threads/{user_id}/messages` и реплая админа в Telegram.
    Возвращает (строку реплики, на сколько устройств ушёл пуш)."""
    row = await db.add_support_message(user_id, "admin", text)
    pushed = await _push_user_reply(user_id, text)
    return row, pushed


def _thread_user_id(request: Request) -> int:
    raw = request.path_params["user_id"]
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(404, "not_found", "no such thread") from exc


async def _require_admin(request: Request) -> int:
    user_id = await common.authed_user_id(request)
    if not is_support_admin(user_id):
        raise ApiError(403, "forbidden", "support inbox is for the support admin only")
    return user_id


# ---------- атлет ----------


async def list_my_messages(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    rows = await db.list_support_messages(user_id)
    return JSONResponse(
        {
            "messages": [message_json(r) for r in rows],
            "unread": await db.support_unread_for_user(user_id),
        }
    )


async def post_my_message(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    text, photo = await api_v1_feedback.read_incoming(request, user_id)
    row = await api_v1_feedback.store_and_forward(user_id, text, photo)
    return JSONResponse({"message": message_json(row)}, status_code=201)


async def mark_my_read(request: Request) -> JSONResponse:
    user_id = await common.authed_user_id(request)
    marked = await db.mark_support_read(user_id, "user")
    return JSONResponse({"marked": marked, "unread": 0})


async def get_photo(request: Request) -> Any:
    """Фото реплики байтами — владельцу ветки и админу. Чужой id отвечает тем
    же 404, что и несуществующий (тот же приём, что у фото истории чата)."""
    user_id = await common.authed_user_id(request)
    row = await db.get_support_message(int(request.path_params["message_id"]))
    if row is None or not row["photo_path"] or (
        row["user_id"] != user_id and not is_support_admin(user_id)
    ):
        raise ApiError(404, "not_found", "no photo for this message")
    path = chat_attachments.path_for(row["photo_path"], root=config.SUPPORT_MEDIA_DIR)
    if path is None:
        raise ApiError(404, "not_found", "no photo for this message")
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(
        path,
        media_type=_PHOTO_CONTENT_TYPES.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=0, must-revalidate"},
    )


# ---------- админ ----------


def _thread_json(row: Any) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        # Лучшее имя, какое есть: ник в Telegram. Имени из Apple сервер не
        # хранит (Apple отдаёт его только приложению), так что у app-only
        # аккаунта здесь null — клиент показывает id.
        "name": row["username"] or None,
        "last_text": row["last_text"],
        "last_from": "user" if row["last_sender"] == "user" else "support",
        "last_at": row["last_at"],
        "unread": int(row["unread"]),
    }


async def list_threads(request: Request) -> JSONResponse:
    await _require_admin(request)
    rows = await db.list_support_threads()
    return JSONResponse({"threads": [_thread_json(r) for r in rows]})


async def list_thread_messages(request: Request) -> JSONResponse:
    await _require_admin(request)
    user_id = _thread_user_id(request)
    rows = await db.list_support_messages(user_id)
    return JSONResponse({"messages": [message_json(r) for r in rows]})


async def post_thread_message(request: Request) -> JSONResponse:
    await _require_admin(request)
    user_id = _thread_user_id(request)
    body = await common.json_body(request)
    text = str(common.require(body, "text", str)).strip()
    if not text:
        raise ApiError(400, "bad_request", "text must not be empty", key="api.error.text_empty")
    if len(text) > MAX_REPLY_LENGTH:
        raise ApiError(
            400, "bad_request", f"text must be at most {MAX_REPLY_LENGTH} characters",
            key="api.error.text_too_long", max=MAX_REPLY_LENGTH,
        )
    if await db.get_user(user_id) is None:
        raise ApiError(404, "not_found", "no such user")
    row, _pushed = await send_admin_reply(user_id, text)
    return JSONResponse({"message": message_json(row)}, status_code=201)


async def mark_thread_read(request: Request) -> JSONResponse:
    await _require_admin(request)
    user_id = _thread_user_id(request)
    marked = await db.mark_support_read(user_id, "admin")
    return JSONResponse({"marked": marked, "unread": 0})


routes = [
    Route("/support/messages", list_my_messages, methods=["GET"]),
    Route("/support/messages", post_my_message, methods=["POST"]),
    Route("/support/read", mark_my_read, methods=["POST"]),
    Route("/support/photos/{message_id:int}", get_photo, methods=["GET"]),
    Route("/support/threads", list_threads, methods=["GET"]),
    Route("/support/threads/{user_id}/messages", list_thread_messages, methods=["GET"]),
    Route("/support/threads/{user_id}/messages", post_thread_message, methods=["POST"]),
    Route("/support/threads/{user_id}/read", mark_thread_read, methods=["POST"]),
]
