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
* Аутентификация — два пути к одной личности (см. mcp_oauth.py): OAuth для
  клиентов, которые подключаются коннектором (claude.ai, Claude Desktop,
  ChatGPT), и статический bearer-токен из `db.mcp_tokens` для тех, где заголовок
  вписывают руками (Claude Code, Cursor, VS Code). Оба приходят в инструмент
  одинаково — `AccessToken.subject` с telegram_id владельца. Проверяет токен
  middleware SDK на транспорте (клиент без токена получает честный 401, а не
  ошибку внутри вызова), а каждый инструмент проверяет ещё раз, что личность
  вообще есть: middleware можно смонтировать неправильно, инструмент — нет.
* Транспорт — streamable HTTP в stateless-режиме: сессию между запросами
  держать негде (Amvera перезапускает контейнер когда угодно), а без неё
  каждый POST самодостаточен.
"""

import logging
from typing import Any

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import ai_trainer
import config
import game_server
import mcp_oauth

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
        "get_muscle_recovery",
        "compare_periods",
    }
)


class Unauthorized(Exception):
    """Запрос без валидного токена. Наружу уходит как ошибка вызова."""


def _user_id() -> int:
    """Чьи данные отдавать — из личности, которую установил middleware SDK.

    `subject` кладут туда оба пути авторизации (см. mcp_oauth), поэтому здесь
    нет ни разбора заголовка, ни знания о том, каким из них пришёл клиент.
    """
    token = get_access_token()
    if token is None or not token.subject:
        # Текст уходит в клиент как есть: это единственная подсказка, которую
        # увидит человек, подключившийся не тем токеном.
        raise Unauthorized(
            "Нужен действующий доступ. Открой бота, набери /mcp и подключи его "
            "заново — коннектором или токеном."
        )
    return int(token.subject)


async def _call(name: str, **arguments: Any) -> str:
    """Общий путь всех инструментов: личность → ридер AI-тренера.

    Возвращает JSON-строку — ровно то же, что видит модель внутри бота.
    """
    if name not in READ_ONLY_TOOLS:
        # Недостижимо через объявленные ниже инструменты; страховка от того,
        # что кто-то добавит вызов мимо белого списка.
        raise ValueError(f"tool {name} is not exposed over MCP")
    user_id = _user_id()
    logger.info("MCP tool %s for user %s", name, user_id)
    return await ai_trainer.execute_tool(user_id, name, arguments)


def build_server() -> MCPServer:
    """Собрать MCP-сервер. Отдельной функцией, чтобы тесты поднимали свой
    экземпляр, а не тянули глобальный.

    `auth_server_provider` без отдельного `token_verifier` — не упущение:
    MCPServer запрещает передавать оба, а из провайдера он сам делает
    верификатор поверх `load_access_token`. Нам это и нужно, потому что там же
    принимается статический токен — один путь проверки на оба вида.
    """
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
        auth_server_provider=mcp_oauth.TrainingLogOAuthProvider(),
        auth=mcp_oauth.auth_settings(MCP_PATH),
    )
    mcp_oauth.register_routes(mcp)
    # Мини-игра живёт на том же сервере: свои роуты без MCP-токена, подлинность
    # пользователя доказывает initData Telegram WebApp (см. game_server).
    game_server.register_routes(mcp)

    @mcp.tool()
    async def get_training_overview() -> str:
        """Сводка по пользователю: единицы измерения, формула e1RM, статистика (всего
        тренировок, за неделю, за 30 дней, дней с последней, стрик) и список его
        упражнений с группой мышц и числом использований. Вызывай первым."""
        return await _call("get_training_overview")

    @mcp.tool()
    async def get_active_workout() -> str:
        """Текущая незавершённая тренировка: когда начата, что уже залогировано,
        заметки к упражнениям. Пустой результат — сейчас пользователь не тренируется."""
        return await _call("get_active_workout")

    @mcp.tool()
    async def list_recent_workouts(limit: int = 5) -> str:
        """Последние завершённые тренировки (1-10, по умолчанию 5): дата, заметки и
        все подходы (вес x повторы) по каждому упражнению. Для вопросов про долгий
        период бери get_full_workout_history."""
        return await _call("list_recent_workouts", limit=max(1, min(int(limit), 10)))

    @mcp.tool()
    async def get_full_workout_history() -> str:
        """Вся история тренировок (до 200 последних) — для вопросов про длинный
        период: динамика за полгода, объём по месяцам, поиск давних тренировок."""
        return await _call("get_full_workout_history")

    @mcp.tool()
    async def get_muscle_recovery() -> str:
        """Насколько отдохнула каждая группа мышц: процент восстановления, когда
        тренировали последний раз и сколько дней назад. 100% значит готова к тяжёлой
        работе, ниже 85% — ещё не отдохнула. Для вопросов «что сегодня качать»."""
        return await _call("get_muscle_recovery")

    @mcp.tool()
    async def get_weekly_volume_by_group() -> str:
        """Объём (рабочие подходы) по группам мышц за текущую и прошлую неделю —
        чем нагрузка перекошена и что недобирает."""
        return await _call("get_weekly_volume_by_group")

    @mcp.tool()
    async def get_exercise_progress(exercise_name: str) -> str:
        """Динамика по одному упражнению: подходы по датам, рабочие веса, e1RM,
        рекорды. exercise_name — точное название из get_training_overview."""
        return await _call("get_exercise_progress", exercise_name=exercise_name)

    @mcp.tool()
    async def list_exercise_catalog() -> str:
        """Каталог упражнений бота по группам мышц — что можно предложить сверх того,
        что пользователь уже делает."""
        return await _call("list_exercise_catalog")

    @mcp.tool()
    async def get_bodyweight_history() -> str:
        """История взвешиваний: дата и вес тела."""
        return await _call("get_bodyweight_history")

    @mcp.tool()
    async def get_food_diary(days: int = 14) -> str:
        """Дневник питания за последние N дней (по умолчанию 14): записи о еде с КБЖУ
        и суточными итогами."""
        return await _call("get_food_diary", days=max(1, min(int(days), 90)))

    @mcp.tool()
    async def get_saved_programs() -> str:
        """Сохранённые программы пользователя: дни, упражнения и схемы подходов."""
        return await _call("get_saved_programs")

    @mcp.tool()
    async def compare_periods(days: int = 90) -> str:
        """Что изменилось по всем упражнениям: последние N дней против предыдущих N
        (по умолчанию 90) — тренировки, подходы, лучший e1RM и тоннаж в каждом окне
        плюс разница, отдельно упражнения, которые бросил или начал делать."""
        return await _call("compare_periods", days=max(7, min(int(days), 365)))

    @mcp.tool()
    async def get_program_adherence() -> str:
        """Насколько реальные тренировки совпадают с сохранённой программой: что
        делается по плану, что пропускается, что добавлено сверху."""
        return await _call("get_program_adherence")

    return mcp


def build_app():
    """ASGI-приложение целиком: MCP на MCP_PATH, рядом — роуты OAuth.

    Своей обёртки с проверкой токена здесь больше нет, и это не упрощение:
    обёртка требовала токен на *любом* запросе, а теперь на том же порту живут
    `/authorize`, `/token`, `/register`, `.well-known` и страница согласия — то
    есть ровно те адреса, куда клиент приходит ещё без токена. Требование токена
    висит на одном `MCP_PATH` (`RequireAuthMiddleware` от SDK), и 401 там такой
    же честный: с `WWW-Authenticate`, по которому клиент находит, где
    авторизоваться.

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
    return mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


async def serve() -> None:
    """Поднять MCP-сервер в текущем event loop (рядом с поллингом, см. main.py).

    Падение сервера не должно ронять бота: Telegram-часть — основная, а MCP
    отвалившийся с ошибкой порта заметят лишь те, кто им пользуется.
    """
    try:
        # Сборка приложения — тоже под перехватом, и это не перестраховка: OAuth
        # требует HTTPS-адреса (RFC 8414), и на MCP_PUBLIC_URL с http сборка
        # падает здесь. Снаружи try это уронило бы задачу с голым трейсбеком, а
        # причина в нём не видна — а бот при этом продолжал бы работать, так что
        # заметить нечем.
        app = build_app()
    except Exception:
        logger.exception(
            "MCP server not started: не удалось собрать приложение. "
            "Проверь MCP_PUBLIC_URL — для OAuth он обязан быть https://"
        )
        return
    server = uvicorn.Server(
        uvicorn.Config(
            app,
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
