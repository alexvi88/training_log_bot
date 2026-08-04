"""MCP-сервер бота: read-only доступ пользователя к собственным данным из
внешних AI-клиентов (Claude Desktop / Claude Code / любой MCP-клиент).

Зачем отдельный сервер, если у AI-тренера уже есть те же инструменты: тренер
живёт внутри диалога в Telegram и отвечает моделью, которую выбрали мы. MCP
разворачивает это наружу — человек открывает свой клиент, подключает бота один
раз и дальше спрашивает про свои тренировки там, где ему удобнее.

Что важно про устройство:

* Инструменты — обёртки над `ai_trainer.execute_tool`, а не отдельная выборка
  из базы. Ридер у пользовательских данных ровно один; иначе «сводка по
  тренировкам» тут и в боте разъезжаются молча, и заметить это можно только
  сравнив два ответа вручную.
* Только чтение. `execute_tool` умеет и писать (`save_athlete_profile`,
  `propose_program`), поэтому имена инструментов здесь перечислены поимённо в
  явном белом списке — новый пишущий инструмент у тренера не должен утечь
  наружу самим фактом своего появления.
* Аутентификация — bearer-токен из `db.mcp_tokens`, выпускаемый в боте
  (handlers/mcp_access.py). Токен и есть личность: он же отвечает на вопрос,
  чьи данные отдавать. Проверяется дважды — ASGI-обёрткой (чтобы клиент без
  токена получил честный 401 на транспорте, а не ошибку внутри вызова) и
  каждым инструментом (обёртку можно смонтировать неправильно, инструмент —
  нет).
* Транспорт — streamable HTTP в stateless-режиме: сессию между запросами
  держать негде (Amvera перезапускает контейнер когда угодно), а без неё
  каждый POST самодостаточен.
"""

import json
import logging
from typing import Any, Optional

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import ai_trainer
import config
import db

logger = logging.getLogger(__name__)

# Путь, на котором отвечает MCP. Совпадает с тем, что показывает экран /mcp.
MCP_PATH = "/mcp"

# Инструменты `ai_trainer.execute_tool`, которые разрешено звать снаружи. Здесь
# нет `get_full_chat_history` (переписка с тренером — самое личное, что есть в
# базе, и вне бота она бесполезна) и нет ничего пишущего.
READ_ONLY_TOOLS = frozenset(
    {
        "get_training_overview",
        "get_active_workout",
        "list_recent_workouts",
        "get_full_workout_history",
        "get_weekly_volume_by_group",
        "get_exercise_progress",
        "list_exercise_catalog",
        "get_bodyweight_history",
        "get_food_diary",
        "get_saved_programs",
        "get_program_adherence",
    }
)


class Unauthorized(Exception):
    """Запрос без валидного токена. Наружу уходит как ошибка вызова."""


def bearer_token(headers: Optional[Any]) -> str:
    """Достать токен из заголовка Authorization.

    Регистр схемы не фиксирован (RFC 7235: `Bearer` — case-insensitive), а
    клиенты пишут её по-разному, поэтому сравниваем в нижнем регистре. Сам
    токен — как есть.
    """
    if not headers:
        return ""
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


async def _user_id(ctx: Context) -> int:
    user_id = await db.resolve_mcp_token(bearer_token(ctx.headers))
    if user_id is None:
        # Текст уходит в клиент как есть: это единственная подсказка, которую
        # увидит человек, вставивший старый или обрезанный токен.
        raise Unauthorized(
            "Нужен действующий токен. Открой бота, набери /mcp и вставь выданный "
            "токен в заголовок Authorization: Bearer <токен>."
        )
    return user_id


async def _call(ctx: Context, name: str, **arguments: Any) -> str:
    """Общий путь всех инструментов: токен → user_id → ридер AI-тренера.

    Возвращает JSON-строку — ровно то же, что видит модель внутри бота.
    """
    if name not in READ_ONLY_TOOLS:
        # Недостижимо через объявленные ниже инструменты; страховка от того,
        # что кто-то добавит вызов мимо белого списка.
        raise ValueError(f"tool {name} is not exposed over MCP")
    user_id = await _user_id(ctx)
    logger.info("MCP tool %s for user %s", name, user_id)
    return await ai_trainer.execute_tool(user_id, name, arguments)


def build_server() -> MCPServer:
    """Собрать MCP-сервер. Отдельной функцией, чтобы тесты поднимали свой
    экземпляр, а не тянули глобальный."""
    mcp = MCPServer(
        name="training-log",
        title="Дневник тренировок",
        instructions=(
            "Данные тренировок одного пользователя Telegram-бота: история тренировок и подходов, "
            "прогресс по упражнениям, недельный объём по группам мышц, вес тела, дневник питания "
            "и сохранённые программы. Всё только на чтение. Начинай с get_training_overview — "
            "оттуда видны единицы измерения (кг/фунты) и точные названия упражнений, которые "
            "нужны остальным инструментам."
        ),
        version="1.0.0",
    )

    @mcp.tool()
    async def get_training_overview(ctx: Context) -> str:
        """Сводка по пользователю: единицы измерения, формула e1RM, статистика (всего
        тренировок, за неделю, за 30 дней, дней с последней, стрик) и список его
        упражнений с группой мышц и числом использований. Вызывай первым."""
        return await _call(ctx, "get_training_overview")

    @mcp.tool()
    async def get_active_workout(ctx: Context) -> str:
        """Текущая незавершённая тренировка: когда начата, что уже залогировано,
        заметки к упражнениям. Пустой результат — сейчас пользователь не тренируется."""
        return await _call(ctx, "get_active_workout")

    @mcp.tool()
    async def list_recent_workouts(ctx: Context, limit: int = 5) -> str:
        """Последние завершённые тренировки (1-10, по умолчанию 5): дата, заметки и
        все подходы (вес x повторы) по каждому упражнению. Для вопросов про долгий
        период бери get_full_workout_history."""
        return await _call(ctx, "list_recent_workouts", limit=max(1, min(int(limit), 10)))

    @mcp.tool()
    async def get_full_workout_history(ctx: Context) -> str:
        """Вся история тренировок (до 200 последних) — для вопросов про длинный
        период: динамика за полгода, объём по месяцам, поиск давних тренировок."""
        return await _call(ctx, "get_full_workout_history")

    @mcp.tool()
    async def get_weekly_volume_by_group(ctx: Context) -> str:
        """Объём (рабочие подходы) по группам мышц за текущую и прошлую неделю —
        чем нагрузка перекошена и что недобирает."""
        return await _call(ctx, "get_weekly_volume_by_group")

    @mcp.tool()
    async def get_exercise_progress(ctx: Context, exercise_name: str) -> str:
        """Динамика по одному упражнению: подходы по датам, рабочие веса, e1RM,
        рекорды. exercise_name — точное название из get_training_overview."""
        return await _call(ctx, "get_exercise_progress", exercise_name=exercise_name)

    @mcp.tool()
    async def list_exercise_catalog(ctx: Context) -> str:
        """Каталог упражнений бота по группам мышц — что можно предложить сверх того,
        что пользователь уже делает."""
        return await _call(ctx, "list_exercise_catalog")

    @mcp.tool()
    async def get_bodyweight_history(ctx: Context) -> str:
        """История взвешиваний: дата и вес тела."""
        return await _call(ctx, "get_bodyweight_history")

    @mcp.tool()
    async def get_food_diary(ctx: Context, days: int = 14) -> str:
        """Дневник питания за последние N дней (по умолчанию 14): записи о еде с КБЖУ
        и суточными итогами."""
        return await _call(ctx, "get_food_diary", days=max(1, min(int(days), 90)))

    @mcp.tool()
    async def get_saved_programs(ctx: Context) -> str:
        """Сохранённые программы пользователя: дни, упражнения и схемы подходов."""
        return await _call(ctx, "get_saved_programs")

    @mcp.tool()
    async def get_program_adherence(ctx: Context) -> str:
        """Насколько реальные тренировки совпадают с сохранённой программой: что
        делается по плану, что пропускается, что добавлено сверху."""
        return await _call(ctx, "get_program_adherence")

    return mcp


def _unauthorized_response() -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(
        {
            "error": "unauthorized",
            "error_description": (
                "Открой Telegram-бота, набери /mcp и передавай выданный токен "
                "в заголовке Authorization: Bearer <токен>."
            ),
        }
    ).encode()
    headers = {
        "content-type": "application/json",
        # RFC 6750: без этого заголовка клиент не знает, что именно от него
        # хотят, и показывает голый 401.
        "www-authenticate": 'Bearer realm="training-log"',
        "content-length": str(len(body)),
    }
    return 401, headers, body


def require_token(app):
    """ASGI-обёртка: без валидного токена дальше запрос не идёт.

    Инструменты и сами проверяют токен, но до вызова инструмента ещё нужно
    пройти `initialize` — а клиент, который успешно инициализировался и только
    потом получил отказ на каждом вызове, выглядит как «сервер сломан», а не
    как «токен не тот». 401 на транспорте — это то, что клиенты умеют
    показывать человеку.
    """

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if await db.resolve_mcp_token(bearer_token(headers)) is None:
            status, response_headers, body = _unauthorized_response()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (k.encode("latin-1"), v.encode("latin-1"))
                        for k, v in response_headers.items()
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    return wrapped


def build_app():
    """ASGI-приложение целиком: MCP на MCP_PATH за проверкой токена.

    stateless_http/json_response: сессию между запросами держать негде — процесс
    один, но перезапускается по воле хостинга, — а SSE-стрим здесь не нужен,
    ответы короткие и приходят целиком.

    DNS-rebinding-защиту (Host/Origin) выключаем явно, и это важно указать
    именно явно: без параметра SDK включает её сам и разрешает только localhost
    — за прокси хостинга приходит Host публичного домена, и каждый запрос
    получает 421. Сама защита здесь и не нужна: она про localhost-серверы, к
    которым браузер жертвы может постучаться от её имени, а тут за каждым
    запросом всё равно должен лежать секрет, которого у чужой страницы нет.
    """
    mcp = build_server()
    return require_token(mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    ))


async def serve() -> None:
    """Поднять MCP-сервер в текущем event loop (рядом с поллингом, см. main.py).

    Падение сервера не должно ронять бота: Telegram-часть — основная, а MCP
    отвалившийся с ошибкой порта заметят лишь те, кто им пользуется.
    """
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(),
            host="0.0.0.0",  # noqa: S104 — контейнер, наружу торчит один порт
            port=config.MCP_PORT,
            log_level="info",
            access_log=False,
        )
    )
    logger.info("MCP server listening on :%s%s", config.MCP_PORT, MCP_PATH)
    try:
        await server.serve()
    except Exception:
        logger.exception("MCP server stopped")
