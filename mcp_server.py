"""MCP-сервер бота: доступ пользователя к собственным данным из внешних
AI-клиентов (Claude Desktop / Claude Code / любой MCP-клиент).

Зачем отдельный сервер, если у AI-тренера уже есть те же инструменты: тренер
живёт внутри диалога в Telegram и отвечает моделью, которую выбрали мы. MCP
разворачивает это наружу — человек открывает свой клиент, подключает бота один
раз и дальше спрашивает про свои тренировки там, где ему удобнее.

Что важно про устройство:

* Инструменты — обёртки над `ai_trainer.execute_tool`, а не отдельная выборка
  из базы. Ридер у пользовательских данных ровно один; иначе «сводка по
  тренировкам» тут и в боте разъезжаются молча, и заметить это можно только
  сравнив два ответа вручную.
* Почти всё чтение. Пишущих инструментов здесь ровно два — `log_bodyweight` и
  `log_food` — и оба выбраны по одному критерию: внутри бота они уже
  «делают сразу и дают откат кнопкой» (`ai_trainer._UNDOABLE_TOOLS`), то есть
  их и так можно вызвать одной репликой без подтверждения. Кнопки отката у
  MCP-клиента нет — `_UNDO_NOTE`, который эти инструменты кладут в payload для
  модели тренера, здесь подменяется на честную подсказку (см. `_call`).
  Остальное пишущее (`save_athlete_profile`, `propose_program`, правки
  программ и упражнений) наружу не идёт: там больше последствий — имена,
  конфликты, структура программы, — и отменяются они не одним тапом. Имена
  инструментов перечислены поимённо в явном белом списке — новый пишущий
  инструмент у тренера не должен утечь наружу самим фактом своего появления.
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

import json
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
# базе, и вне бота она бесполезна) и нет ничего пишущего, кроме WRITE_TOOLS
# ниже — того, что и так делается сразу и без подтверждения внутри бота.
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

# Пишущие инструменты, разрешённые снаружи, — подмножество
# `ai_trainer._UNDOABLE_TOOLS`, отобранное по двум критериям:
#
# 1. Добавление, а не правка существующего: log_bodyweight, log_food,
#    create_exercise, copy_program сами описаны как «ДЕЛАЕТ СРАЗУ — добавление
#    ничего не портит». Новую строку легко найти и убрать вручную, если она
#    лишняя.
# 2. Удаление с полным восстановлением: delete_food_entry и
#    delete_bodyweight_log удаляют конкретную запись (id — из get_food_diary /
#    get_bodyweight_history), но их undo не «намекает вернуться и переделать»,
#    а честно воссоздаёт ту же строку с теми же значениями (kind=
#    food_restore/bodyweight_restore в handlers.ai_trainer._apply_undo). Это
#    ближе к «сходить и обратно», чем к «мутации задним числом».
#
# Кнопки отката у MCP-клиента нет, поэтому `_call` подменяет `note` в ответе
# (см. ниже) — вернуть удалённое всё равно можно, попросив то же самое ещё раз
# в чате с тренером или в самом боте.
#
# rename_exercise, move_exercise_to_group и rename_program сюда осознанно НЕ
# идут — это мутация БЕЗ восстановления исходного значения (откат меняет само
# имя обратно, а не «отменяет» правку симметрично): переименовать не то
# упражнение или программу снаружи легче, чем через бота (там подсказки,
# автодополнение по точному имени).
WRITE_TOOLS = frozenset({
    "log_bodyweight", "log_food", "create_exercise", "copy_program",
    "delete_food_entry", "delete_bodyweight_log",
})

EXPOSED_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS

# Инструменту нечего сказать про кнопку, которой здесь нет — см. WRITE_TOOLS.
_MCP_WRITE_NOTE = (
    "Записано. Поправить или убрать эту запись можно в самом боте: "
    "🏋️ Дневник веса или 🍽 Дневник еды."
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


async def _call(tool_name: str, **arguments: Any) -> str:
    """Общий путь всех инструментов: личность → ридер/писатель AI-тренера.

    Параметр называется `tool_name`, а не `name` — у create_exercise и
    copy_program среди своих же аргументов есть `name` (название упражнения
    / программы), и с `_call(name: str, **arguments)` вызов `_call("create_
    exercise", name=name, group=group)` падал с «got multiple values for
    argument 'name'»: kwarg `name` от инструмента сталкивался с позиционным
    именем самого инструмента. Ловится только вызовом по проводу — с прямым
    вызовом `execute_tool` в тестах коллизии нет, эту ошибку словил только
    сквозной JSON-RPC тест.

    Возвращает JSON-строку — то, что видит модель внутри бота, с одной
    поправкой: у WRITE_TOOLS `note` рассчитан на модель тренера в Telegram
    («под ответом кнопка отката») — здесь такой кнопки нет, и тот же текст
    внешнему клиенту был бы неправдой.

    Заменяем подстрокой, а не блокирующей перезаписью всего поля: у части
    инструментов `note` — это `_UNDO_NOTE` целиком (log_bodyweight,
    log_food, create_exercise на успехе), а у copy_program — префикс плюс
    он же («Копия «X» создана. {_UNDO_NOTE}»). У тех же create_exercise
    бывают и совсем другие note («такое упражнение уже есть», «выбери
    группу из списка») — их подмена исказила бы смысл, поэтому трогаем
    только когда в тексте реально встретился `_UNDO_NOTE`.
    """
    if tool_name not in EXPOSED_TOOLS:
        # Недостижимо через объявленные ниже инструменты; страховка от того,
        # что кто-то добавит вызов мимо белого списка.
        raise ValueError(f"tool {tool_name} is not exposed over MCP")
    user_id = _user_id()
    logger.info("MCP tool %s for user %s", tool_name, user_id)
    raw = await ai_trainer.execute_tool(user_id, tool_name, arguments)
    if tool_name not in WRITE_TOOLS:
        return raw
    payload = json.loads(raw)
    note = payload.get("note")
    if isinstance(note, str) and ai_trainer._UNDO_NOTE in note:
        payload["note"] = note.replace(ai_trainer._UNDO_NOTE, _MCP_WRITE_NOTE)
    return json.dumps(payload, ensure_ascii=False)


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
            "и сохранённые программы. Почти всё только на чтение — кроме log_bodyweight, "
            "log_food, create_exercise, copy_program, delete_food_entry и "
            "delete_bodyweight_log: они пишут сразу и без подтверждения (переименование "
            "и другая правка — только в самом боте). Начинай с get_training_overview — "
            "оттуда видны единицы измерения (кг/фунты) и точные названия упражнений, "
            "которые нужны остальным инструментам."
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

    @mcp.tool()
    async def log_bodyweight(weight: float) -> str:
        """Записать вес тела в дневник — пишет сразу, без подтверждения. Число в
        единицах пользователя (unit из get_training_overview), без конвертации.
        Поправить или убрать запись — в самом боте, 🏋️ Дневник веса."""
        return await _call("log_bodyweight", weight=weight)

    @mcp.tool()
    async def log_food(
        description: str,
        calories: float | None = None,
        protein: float | None = None,
        fat: float | None = None,
        carbs: float | None = None,
    ) -> str:
        """Записать съеденное в дневник питания за сегодня — пишет сразу, без
        подтверждения. КБЖУ передавай, только если их назвал сам человек; не
        знаешь — оставь пустыми, оценит тот же разборщик, что и на экране
        дневника. Поправить или убрать запись — в самом боте, 🍽 Дневник еды."""
        return await _call(
            "log_food", description=description,
            calories=calories, protein=protein, fat=fat, carbs=carbs,
        )

    @mcp.tool()
    async def create_exercise(name: str, group: str) -> str:
        """Завести своё упражнение, которого нет ни в списке пользователя, ни в
        каталоге — пишет сразу, добавление ничего не портит. Сначала проверь
        get_training_overview и list_exercise_catalog: если движение уже есть,
        бери его, а не заводи второе с чуть другим названием. group — точное
        имя группы мышц из get_training_overview или list_exercise_catalog."""
        return await _call("create_exercise", name=name, group=group)

    @mcp.tool()
    async def copy_program(name: str, new_name: str | None = None) -> str:
        """Дубликат сохранённой программы со всеми днями, упражнениями и схемами —
        пишет сразу, копия ничего не портит. name — точное имя источника из
        get_saved_programs. Без new_name имя возьмётся свободное рядом с
        исходным («PPL (2)»)."""
        return await _call("copy_program", name=name, new_name=new_name)

    @mcp.tool()
    async def delete_food_entry(entry_id: int) -> str:
        """Убрать одну запись из дневника питания — пишет сразу. entry_id — точный
        id из get_food_diary, не выдумывай; если человек не назвал, какую запись,
        посмотри дневник и уточни, если записей за день несколько."""
        return await _call("delete_food_entry", entry_id=entry_id)

    @mcp.tool()
    async def delete_bodyweight_log(log_id: int) -> str:
        """Убрать одну запись веса — пишет сразу. log_id — точный id из
        get_bodyweight_history, не выдумывай."""
        return await _call("delete_bodyweight_log", log_id=log_id)

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
