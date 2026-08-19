"""Мини-игры Telegram Mini App: страницы и приём результатов.

Схема повторяет beat the owl из english-audio-bot: страница отдаётся тем же
HTTP-сервером, что и MCP (наружу из контейнера торчит один порт, см.
mcp_server.serve), результат приходит POST'ом с initData Telegram WebApp,
которое проверяется подписью бота. Роуты вешаются через custom_route — без
требования MCP-токена: у страницы игры токена нет и быть не может, подлинность
пользователя доказывает initData.

Игр несколько (slug -> GameSpec в GAMES): «Кач-Раннер» (runner, оригинальная
игра) и «Кач-Отряд» (squad, добавлена вторым слотом). /game и /game-result —
общие для всех игр URL'ы, чтобы старые ссылки и кнопки в чатах не умирали;
какая именно игра имеется в виду, решает путь для страницы и поле "game" в
теле запроса для результата (отсутствие поля = "runner" — так шлёт старый
клиент, который про это поле не знает).
"""

import dataclasses
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
import i18n

logger = logging.getLogger(__name__)

GAME_PATH = "/game"
RESULT_PATH = "/game-result"
# /game/squad, а не вложенный под /game путь-параметр: два независимых
# статических роута, starlette матчит их по точному совпадению, конфликтов с
# /game нет (в отличие от него страница отряда ничего не параметризует).
SQUAD_PATH = "/game/squad"
# Статика игр: модели толпы (.glb), three.js и постобработка (см.
# assets/game/README.md). Персонажей страницы рисуют сами — картинок для
# карточек выбора здесь больше нет.
ASSETS_ROUTE = "/assets/game/{filename}"

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets" / "game"

GAME_RUNNER = "runner"
GAME_SQUAD = "squad"

# initData старше 4 часов не принимаем — защита от реплея. Не короче: человек
# мог открыть игру и отойти, а легитимный результат с 403 терять обидно
# (та же логика и цифры, что в beat the owl). Часы клиента могут спешить.
INIT_DATA_TTL = 4 * 3600
CLOCK_SKEW = 300

# Санити-границы: столько честно не набегать и не насобирать — всё сверх
# означает подделанный запрос, а не игру.
MAX_DISTANCE = 50_000
MAX_SCORE = 100_000
MAX_SQUAD = 5_000


@dataclasses.dataclass(frozen=True)
class GameSpec:
    slug: str
    page_file: Path
    page_path: str


GAMES: dict[str, GameSpec] = {
    GAME_RUNNER: GameSpec(GAME_RUNNER, BASE_DIR / "game.html", GAME_PATH),
    GAME_SQUAD: GameSpec(GAME_SQUAD, BASE_DIR / "game_squad.html", SQUAD_PATH),
}


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


def parse_result(raw: Any, game: str = GAME_RUNNER) -> Optional[dict]:
    """Результат забега из тела запроса, проверенный по типам и границам.

    Формат общий для обеих игр (раннер просто не заполняет squad — 0 по
    умолчанию), различаются только границы: у отряда есть свой потолок на
    размер отряда, которого у раннера нет.
    """
    if game not in GAMES:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        distance = int(raw.get("distance", -1))
        score = int(raw.get("score", -1))
        squad = int(raw.get("squad", 0))
    except (TypeError, ValueError):
        return None
    if not (0 <= distance <= MAX_DISTANCE and 0 <= score <= MAX_SCORE):
        return None
    if not (0 <= squad <= MAX_SQUAD):
        return None
    return {
        "distance": distance,
        "score": score,
        "squad": squad,
        "fighter": str(raw.get("fighter", ""))[:16],
    }


# Дедуп повторной отправки (ретрай сети, двойной тап по «Закрыть»): ключ —
# пользователь + игра + клиентский timestamp забега, окно минутное. Потерять
# один честный результат из-за совпадения ключей невозможно: timestamp у
# клиента ставится на каждый забег заново.
_processed: dict[str, float] = {}
_DEDUP_TTL = 60.0


def _is_duplicate(user_id: int, game: str, raw: Any) -> bool:
    stamp = ""
    if isinstance(raw, dict):
        stamp = str(raw.get("gameTimestamp", ""))
    key = f"{user_id}:{game}:{stamp}"
    now = time.time()
    for k in [k for k, ts in list(_processed.items()) if now - ts > _DEDUP_TTL]:
        _processed.pop(k, None)
    if key in _processed:
        return True
    _processed[key] = now
    return False


async def game_page(request: Request):
    return FileResponse(GAMES[GAME_RUNNER].page_file, media_type="text/html")


async def game_squad_page(request: Request):
    return FileResponse(GAMES[GAME_SQUAD].page_file, media_type="text/html")


async def game_asset(request: Request):
    filename = request.path_params["filename"]
    path = ASSETS_DIR / filename
    # Без обхода каталога: имя — ровно один сегмент внутри assets/game.
    if "/" in filename or "\\" in filename or ".." in filename or not path.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path)


# Реакция тренера в чате. Не на каждый забег (это спам), а на события:
# первый забег вообще и новый рекорд — заметный, иначе ранние забеги, каждый
# из которых чуть лучше предыдущего, зафлудили бы чат. У раннера рекорд — это
# метры, у отряда — очки (метры отряд тоже копит, но геймплей вокруг очков:
# блины с числами и ворота ×2 значат больше пройденной дистанции).
RECORD_MIN_DISTANCE = 300
RECORD_MIN_GAIN = 1.2
RECORD_MIN_SCORE = 300
RECORD_MIN_SCORE_GAIN = 1.2

def _trainer_reaction(distance: int, best_before: int) -> Optional[str]:
    """Текст тренера про забег раннера или None, когда событие того не стоит.

    Вызывается из process_game_result уже внутри `i18n.use_lang(...)` — сам
    текст берётся из каталога через `i18n.t()`, а не строкой в этом модуле:
    языка на месте вызова нет ни в контексте апдейта, ни где-либо ещё, кроме
    БД (см. комментарий у process_game_result).
    """
    if best_before == 0:
        return i18n.t("game.first_run", distance=distance)
    if distance >= RECORD_MIN_DISTANCE and distance >= best_before * RECORD_MIN_GAIN:
        return i18n.t("game.record", distance=distance, best=best_before)
    return None


def _squad_trainer_reaction(score: int, squad: int, best_before: int) -> Optional[str]:
    """Текст тренера про забег отряда или None, когда событие того не стоит."""
    if best_before == 0:
        return i18n.t("game.squad_first_run", score=score, squad=squad)
    if score >= RECORD_MIN_SCORE and score >= best_before * RECORD_MIN_SCORE_GAIN:
        return i18n.t("game.squad_record", score=score, best=best_before)
    return None


async def _send_trainer_message(user_id: int, text: str) -> None:
    """Отправить сообщение от бота вне обработчика апдейта.

    Свой короткоживущий Bot: у game_server нет доступа к инстансу из main
    (роуты регистрируются при сборке MCP-приложения, бота там ещё нет), а
    событие редкое — первый забег да рекорды, сессию не жалко.
    """
    from aiogram import Bot

    bot = Bot(token=config.BOT_TOKEN)
    try:
        await bot.send_message(user_id, text)
    finally:
        await bot.session.close()


async def process_game_result(user_id: int, raw: Any, game: str = GAME_RUNNER) -> bool:
    """Сохранить результат забега и, если событие того стоит, ответить тренером.

    False — результат не прошёл проверку границ или игра неизвестна; дубликат
    считается успехом (клиент ретраит сеть, второй раз записывать нечего).

    Реплика тренера уходит из своего короткоживущего `Bot` (см.
    _send_trainer_message) вне обработчика апдейта — там, где `i18n.set_lang`
    вообще не вызывается, а значит без явной подстраховки язык был бы
    случайным (тем, что остался в contextvar с прошлого запроса в этом же
    треде). Тот же приём, что и в ai_trainer.weekly_digest и в рассылке
    пушей: язык берём из БД по telegram_id и оборачиваем рендер в
    `i18n.use_lang(...)`.
    """
    result = parse_result(raw, game=game)
    if result is None:
        return False
    if _is_duplicate(user_id, game, raw):
        return True
    if game == GAME_SQUAD:
        best_before = await db.get_squad_best_score(user_id)
    else:
        best_before = await db.get_game_best_distance(user_id)
    await db.save_game_result(
        user_id, result["distance"], result["score"], result["fighter"],
        game=game, squad=result["squad"],
    )
    logger.info(
        "game result: user=%s game=%s distance=%s score=%s squad=%s",
        user_id, game, result["distance"], result["score"], result["squad"],
    )
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else i18n.DEFAULT_LANG
    with i18n.use_lang(lang):
        if game == GAME_SQUAD:
            text = _squad_trainer_reaction(result["score"], result["squad"], best_before)
        else:
            text = _trainer_reaction(result["distance"], best_before)
    if text:
        try:
            await _send_trainer_message(user_id, text)
        except Exception as e:
            # Сообщение — приятный бонус, а не часть контракта: результат уже
            # записан, и клиенту незачем получать 500 из-за упавшей отправки.
            logger.warning("game result notify failed: user=%s: %s", user_id, e)
    return True


async def game_result(request: Request):
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("bad json", status_code=400)
    user_id = validate_init_data(str(body.get("initData", "")))
    if not user_id:
        return PlainTextResponse("unauthorized", status_code=403)
    game = str(body.get("game") or GAME_RUNNER)
    if game not in GAMES:
        return PlainTextResponse("unknown game", status_code=400)
    if not await process_game_result(user_id, body.get("result"), game=game):
        return PlainTextResponse("invalid result", status_code=400)
    return PlainTextResponse("ok")


def register_routes(server: Any) -> None:
    """Повесить роуты игр на приложение MCP-сервера (см. mcp_server.build_server)."""
    server.custom_route(GAME_PATH, methods=["GET"])(game_page)
    server.custom_route(SQUAD_PATH, methods=["GET"])(game_squad_page)
    server.custom_route(RESULT_PATH, methods=["POST"])(game_result)
    server.custom_route(ASSETS_ROUTE, methods=["GET"])(game_asset)
