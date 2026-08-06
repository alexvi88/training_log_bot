"""Мини-игра «Кач-Раннер»: страница Telegram Mini App и приём результатов.

Схема повторяет beat the owl из english-audio-bot: страница отдаётся тем же
HTTP-сервером, что и MCP (наружу из контейнера торчит один порт, см.
mcp_server.serve), результат приходит POST'ом с initData Telegram WebApp,
которое проверяется подписью бота. Роуты вешаются через custom_route — без
требования MCP-токена: у страницы игры токена нет и быть не может, подлинность
пользователя доказывает initData.
"""

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse

import config
import db

logger = logging.getLogger(__name__)

GAME_PATH = "/game"
RESULT_PATH = "/game-result"
# Арты карточек персонажей (см. assets/game/README.md); страница переживает их
# отсутствие — рисует векторных атлетов сама.
ASSETS_ROUTE = "/assets/game/{filename}"

BASE_DIR = Path(__file__).resolve().parent
PAGE_FILE = BASE_DIR / "game.html"
ASSETS_DIR = BASE_DIR / "assets" / "game"

# initData старше 4 часов не принимаем — защита от реплея. Не короче: человек
# мог открыть игру и отойти, а легитимный результат с 403 терять обидно
# (та же логика и цифры, что в beat the owl). Часы клиента могут спешить.
INIT_DATA_TTL = 4 * 3600
CLOCK_SKEW = 300

# Санити-границы: столько честно не набегать и не насобирать — всё сверх
# означает подделанный запрос, а не игру.
MAX_DISTANCE = 50_000
MAX_SCORE = 100_000


def validate_init_data(init_data_str: str) -> Optional[int]:
    """Проверить подпись Telegram WebApp initData; вернуть user_id или None."""
    try:
        if not init_data_str:
            return None
        params = dict(urllib.parse.parse_qsl(init_data_str, keep_blank_values=True))
        hash_value = params.pop("hash", None)
        if not hash_value:
            return None
        auth_date = int(params.get("auth_date", 0))
        now = time.time()
        # Токены «из будущего» тоже отбрасываем: клиент с сильно убежавшими
        # вперёд часами дал бы initData, который не протухает никогда.
        if auth_date > now + CLOCK_SKEW:
            logger.warning("game initData: future auth_date=%d", auth_date)
            return None
        if now - auth_date > INIT_DATA_TTL:
            logger.warning("game initData: stale auth_date=%d", auth_date)
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.HMAC(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.HMAC(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_value):
            logger.warning("game initData: hash mismatch")
            return None
        user = json.loads(params.get("user", "{}"))
        uid = user.get("id")
        return int(uid) if uid else None
    except Exception as e:
        logger.warning("game initData: %s", e)
        return None


def parse_result(raw: Any) -> Optional[dict]:
    """Результат забега из тела запроса, проверенный по типам и границам."""
    if not isinstance(raw, dict):
        return None
    try:
        distance = int(raw.get("distance", -1))
        score = int(raw.get("score", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= distance <= MAX_DISTANCE and 0 <= score <= MAX_SCORE):
        return None
    return {"distance": distance, "score": score, "fighter": str(raw.get("fighter", ""))[:16]}


# Дедуп повторной отправки (ретрай сети, двойной тап по «Закрыть»): ключ —
# пользователь + клиентский timestamp забега, окно минутное. Потерять один
# честный результат из-за совпадения ключей невозможно: timestamp у клиента
# ставится на каждый забег заново.
_processed: dict[str, float] = {}
_DEDUP_TTL = 60.0


def _is_duplicate(user_id: int, raw: Any) -> bool:
    stamp = ""
    if isinstance(raw, dict):
        stamp = str(raw.get("gameTimestamp", ""))
    key = f"{user_id}:{stamp}"
    now = time.time()
    for k in [k for k, ts in list(_processed.items()) if now - ts > _DEDUP_TTL]:
        _processed.pop(k, None)
    if key in _processed:
        return True
    _processed[key] = now
    return False


async def game_page(request: Request):
    return FileResponse(PAGE_FILE, media_type="text/html")


async def game_asset(request: Request):
    filename = request.path_params["filename"]
    path = ASSETS_DIR / filename
    # Без обхода каталога: имя — ровно один сегмент внутри assets/game.
    if "/" in filename or "\\" in filename or ".." in filename or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path)


async def game_result(request: Request):
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    user_id = validate_init_data(str(body.get("initData", "")))
    if not user_id:
        return PlainTextResponse("unauthorized", status_code=403)
    result = parse_result(body.get("result"))
    if result is None:
        return PlainTextResponse("invalid result", status_code=400)
    if _is_duplicate(user_id, body.get("result")):
        return PlainTextResponse("ok")
    await db.save_game_result(user_id, result["distance"], result["score"], result["fighter"])
    logger.info("game result: user=%s distance=%s score=%s", user_id, result["distance"], result["score"])
    return PlainTextResponse("ok")


def register_routes(server: Any) -> None:
    """Повесить роуты игры на приложение MCP-сервера (см. mcp_server.build_server)."""
    server.custom_route(GAME_PATH, methods=["GET"])(game_page)
    server.custom_route(RESULT_PATH, methods=["POST"])(game_result)
    server.custom_route(ASSETS_ROUTE, methods=["GET"])(game_asset)
