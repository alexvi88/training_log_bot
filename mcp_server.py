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

import asyncio
import json
import logging
from typing import Any

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import ai_trainer
import config
import db
import game_server
import i18n
import mcp_oauth

logger = logging.getLogger(__name__)

# Путь, на котором отвечает MCP. Совпадает с тем, что показывает экран /mcp.
MCP_PATH = "/mcp"

# Пауза перед перезапуском упавшего server.serve() — не мгновенно, чтобы
# падение на каждой попытке (например, порт ещё не освободился) не крутило
# цикл вхолостую и не заливало лог.
MCP_RESTART_DELAY_SECONDS = 5

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
#
# Без имени конкретного экрана: первая версия говорила «поправить можно в
# 🏋️ Дневник веса или 🍽 Дневник еды» — текст, скопированный с log_bodyweight/
# log_food и подставлявшийся ВСЕМ шести WRITE_TOOLS. create_exercise ответил
# им же — «поправить упражнение можно в Дневнике веса» неправда, у него
# вообще нет отдельного экрана. Общая формулировка верна для всех шести.
async def _mcp_write_note(user_id: int) -> str:
    """Приписка к успешной записи — уходит в клиент ДАННЫМИ (поле `note` в JSON),
    а не описанием инструмента, поэтому язык у неё пользовательский.

    Описания инструментов в этом файле одноязычно английские осознанно: схема
    забирается один раз на процесс, и сигнала языка там нет. Здесь сигнал есть —
    user_id к этому моменту уже разрешён, — так что общее правило файла на эту
    строку не распространяется. Язык берём из базы явно: MCP-вызов приходит не
    апдейтом Telegram, никакая middleware контекст не выставляла, и полагаться
    на i18n.get_lang() тут значило бы отдать язык предыдущего вызова.
    """
    user = await db.get_user(user_id)
    lang = user["lang"] if user is not None else i18n.DEFAULT_LANG
    return i18n.t_in(lang, "mcp.write_note")


class Unauthorized(Exception):
    """Запрос без валидного токена. Наружу уходит как ошибка вызова."""


def _user_id() -> int:
    """Whose data to return — from the identity the SDK middleware set.

    Both auth paths (see mcp_oauth) put `subject` there, so there's no header
    parsing here, nor any need to know which path the client came through.
    """
    token = get_access_token()
    if token is None or not token.subject:
        # Текст уходит в клиент как есть: это единственная подсказка, которую
        # увидит человек, подключившийся не тем токеном.
        #
        # Двуязычный одной строкой, а не по языку пользователя: эта ветка
        # срабатывает именно тогда, когда мы НЕ знаем, кто пришёл, — токен
        # негодный, user_id нет, и спросить язык не у кого. Тот же приём, что на
        # экране выбора языка в онбординге (screen.onboarding_language.title):
        # когда сигнала нет, честнее показать оба языка, чем угадать один.
        raise Unauthorized(
            "Access expired. Open the bot, send /mcp and connect it again — "
            "via connector or token.\n"
            "Нужен действующий доступ. Открой бота, набери /mcp и подключи его "
            "заново — коннектором или токеном."
        )
    return int(token.subject)


async def _call(tool_name: str, **arguments: Any) -> str:
    """Common path for every tool: identity → the AI trainer's reader/writer.

    The parameter is named `tool_name`, not `name` — create_exercise and
    copy_program both have their own `name` argument (exercise/program title),
    and with `_call(name: str, **arguments)` a call like `_call("create_
    exercise", name=name, group=group)` failed with "got multiple values for
    argument 'name'": the tool's own `name` kwarg collided with the tool's own
    positional name. Only a call over the wire catches this — calling
    `execute_tool` directly in tests has no collision, so only the end-to-end
    JSON-RPC test caught this bug.

    Returns a JSON string — what the model sees inside the bot, with one
    adjustment: for WRITE_TOOLS, `note` is written for the trainer model in
    Telegram ("there's an undo button below this reply") — there's no such
    button here, and the same text would be a lie to an external client.

    We replace a substring rather than overwriting the whole field: for some
    tools `note` IS `_UNDO_NOTE` verbatim (log_bodyweight, log_food,
    create_exercise on success), while copy_program's is a prefix plus the
    same note ("Copy of "X" created. {_UNDO_NOTE}"). Those same create_exercise
    calls can also return entirely different notes ("this exercise already
    exists", "pick a group from the list") — overwriting those would distort
    their meaning, so we only touch the text when `_UNDO_NOTE` actually
    appears in it.
    """
    if tool_name not in EXPOSED_TOOLS:
        # Недостижимо через объявленные ниже инструменты; страховка от того,
        # что кто-то добавит вызов мимо белого списка.
        raise ValueError(f"tool {tool_name} is not exposed over MCP")
    user_id = _user_id()
    logger.info("MCP tool %s for user %s", tool_name, user_id)
    raw = await ai_trainer.execute_tool(user_id, tool_name, arguments, source="mcp")
    if tool_name not in WRITE_TOOLS:
        return raw
    payload = json.loads(raw)
    note = payload.get("note")
    if isinstance(note, str) and ai_trainer._UNDO_NOTE in note:
        payload["note"] = note.replace(ai_trainer._UNDO_NOTE, await _mcp_write_note(user_id))
    return json.dumps(payload, ensure_ascii=False)


def build_server() -> MCPServer:
    """Build the MCP server. A separate function so tests spin up their own
    instance instead of pulling in the global one.

    `auth_server_provider` with no separate `token_verifier` is not an
    oversight: MCPServer refuses to accept both, and it builds a verifier out
    of the provider itself, on top of `load_access_token`. That's exactly what
    we need, because that same method also accepts the static token — one
    verification path for both kinds.
    """
    mcp = MCPServer(
        name="training-log",
        title="Training Log",
        instructions=(
            "Training data for one Telegram bot user: workout and set history, exercise "
            "progress, weekly volume by muscle group, body weight, food diary, and saved "
            "programs. Almost everything is read-only — except log_bodyweight, log_food, "
            "create_exercise, copy_program, delete_food_entry, and delete_bodyweight_log: "
            "these write immediately, without confirmation (renaming and other edits are "
            "bot-only). Start with get_training_overview — it shows the units (kg/lb) and "
            "the exact exercise names the other tools need."
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
        """User summary: units, e1RM formula, stats (total workouts, this week, last 30
        days, days since last one, streak) and their exercise list with muscle group and
        use count. Call this first."""
        return await _call("get_training_overview")

    @mcp.tool()
    async def get_active_workout() -> str:
        """The current unfinished workout: when it started, what's logged so far, and
        exercise notes. An empty result means the user isn't training right now."""
        return await _call("get_active_workout")

    @mcp.tool()
    async def list_recent_workouts(limit: int = 5) -> str:
        """The most recent completed workouts (1-10, default 5): date, notes, and every
        set (weight x reps) for each exercise. For questions about a long time span, use
        get_full_workout_history instead."""
        return await _call("list_recent_workouts", limit=max(1, min(int(limit), 10)))

    @mcp.tool()
    async def get_full_workout_history() -> str:
        """The full workout history (up to the last 200) — for questions about a long
        time span: trends over six months, monthly volume, finding old workouts."""
        return await _call("get_full_workout_history")

    @mcp.tool()
    async def get_muscle_recovery() -> str:
        """How recovered each muscle group is: recovery percentage, when it was last
        trained, and how many days ago. 100% means ready for heavy work, below 85% means
        it hasn't recovered yet. Use this for "what should I train today" questions."""
        return await _call("get_muscle_recovery")

    @mcp.tool()
    async def get_weekly_volume_by_group() -> str:
        """Volume (working sets) by muscle group for the current and previous week —
        where the load is lopsided and what's falling short."""
        return await _call("get_weekly_volume_by_group")

    @mcp.tool()
    async def get_exercise_progress(exercise_name: str) -> str:
        """Progress on a single exercise: sets by date, working weights, e1RM, records.
        exercise_name is the exact name from get_training_overview. If total_sessions is
        0 (sessions is an empty list), the exercise has no logged entries at all — don't
        make up a date, sets, or e1RM for it; say plainly that there's no data yet."""
        return await _call("get_exercise_progress", exercise_name=exercise_name)

    @mcp.tool()
    async def list_exercise_catalog() -> str:
        """The bot's exercise catalog by muscle group — what could be suggested beyond
        what the user already does."""
        return await _call("list_exercise_catalog")

    @mcp.tool()
    async def get_bodyweight_history() -> str:
        """Weigh-in history: date and body weight."""
        return await _call("get_bodyweight_history")

    @mcp.tool()
    async def get_food_diary(days: int = 14) -> str:
        """Food diary for the last N days (default 14): food entries with calories/
        protein/fat/carbs and daily totals."""
        return await _call("get_food_diary", days=max(1, min(int(days), 90)))

    @mcp.tool()
    async def get_saved_programs() -> str:
        """The user's saved programs: days, exercises, and set schemes."""
        return await _call("get_saved_programs")

    @mcp.tool()
    async def compare_periods(days: int = 90) -> str:
        """What changed across all exercises: the last N days against the previous N
        (default 90) — workouts, sets, best e1RM, and tonnage in each window plus the
        difference, and separately, exercises that were dropped or newly started."""
        return await _call("compare_periods", days=max(7, min(int(days), 365)))

    @mcp.tool()
    async def get_program_adherence() -> str:
        """How closely actual workouts match the saved program: what's being done on
        plan, what's being skipped, what's been added on top."""
        return await _call("get_program_adherence")

    @mcp.tool()
    async def log_bodyweight(weight: float) -> str:
        """Log a body weight entry — writes immediately, no confirmation. The number is
        in the user's unit (see `unit` in get_training_overview), no conversion is done.
        Fixing or removing the entry is bot-only, in the Weight Log screen."""
        return await _call("log_bodyweight", weight=weight)

    @mcp.tool()
    async def log_food(
        description: str,
        calories: float | None = None,
        protein: float | None = None,
        fat: float | None = None,
        carbs: float | None = None,
    ) -> str:
        """Log something eaten today into the food diary — writes immediately, no
        confirmation. Only pass calories/protein/fat/carbs if the person actually stated
        them; leave them blank if you don't know — the same estimator used on the diary
        screen will fill them in. Fixing or removing the entry is bot-only, in the Food
        Diary screen."""
        return await _call(
            "log_food", description=description,
            calories=calories, protein=protein, fat=fat, carbs=carbs,
        )

    @mcp.tool()
    async def create_exercise(name: str, group: str) -> str:
        """Add a custom exercise that's in neither the user's list nor the catalog —
        writes immediately, adding never breaks anything. Check get_training_overview
        and list_exercise_catalog first: if the movement already exists, use that one
        instead of creating a second one under a slightly different name. group is the
        exact muscle group name from get_training_overview or list_exercise_catalog."""
        return await _call("create_exercise", name=name, group=group)

    @mcp.tool()
    async def copy_program(name: str, new_name: str | None = None) -> str:
        """Duplicate a saved program with all its days, exercises, and set schemes —
        writes immediately, a copy never breaks anything. name is the exact source name
        from get_saved_programs. Without new_name, a free name near the original is
        picked automatically ("PPL (2)")."""
        return await _call("copy_program", name=name, new_name=new_name)

    @mcp.tool()
    async def delete_food_entry(entry_id: int) -> str:
        """Remove one food diary entry — writes immediately. entry_id is the exact id
        from get_food_diary, don't guess it; if the person didn't say which entry, check
        the diary and ask if there's more than one entry for that day."""
        return await _call("delete_food_entry", entry_id=entry_id)

    @mcp.tool()
    async def delete_bodyweight_log(log_id: int) -> str:
        """Remove one body weight entry — writes immediately. log_id is the exact id
        from get_bodyweight_history, don't guess it."""
        return await _call("delete_bodyweight_log", log_id=log_id)

    return mcp


def build_app():
    """The whole ASGI app: MCP on MCP_PATH, with the OAuth routes alongside it.

    There's no custom token-checking wrapper anymore, and that's not a
    simplification for its own sake: the old wrapper required a token on
    *every* request, and now the same port also serves `/authorize`, `/token`,
    `/register`, `.well-known`, and the consent page — exactly the addresses a
    client hits before it has a token at all. The token requirement now lives
    on `MCP_PATH` alone (the SDK's `RequireAuthMiddleware`), and its 401 is just
    as honest: it carries `WWW-Authenticate`, which tells the client where to
    authorize.

    stateless_http/json_response: there's nowhere to hold a session between
    requests — it's one process, but the host can restart it at will — and an
    SSE stream isn't needed here; responses are short and arrive whole.

    DNS-rebinding protection (Host/Origin) is disabled explicitly, and that's
    worth stating explicitly: without the parameter the SDK turns it on itself
    and allows only localhost — behind the host's proxy every request arrives
    with the public domain's Host header, and each one would get a 421. The
    protection itself isn't needed here anyway: it guards against localhost
    servers a victim's browser could reach on their behalf, and here every
    request still needs a secret that a random page doesn't have.
    """
    mcp = build_server()
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # SDK's dynamic client registration (RFC 7591) caps only metadata size
    # (see TrainingLogOAuthProvider.register_client / db.OAUTH_CLIENT_METADATA_LIMIT),
    # not request frequency — an anonymous flood can grow oauth_clients for hours
    # before the once-a-day prune catches up.
    return mcp_oauth.RegisterRateLimitMiddleware(app)


async def serve() -> None:
    """Bring up the MCP server on the current event loop (alongside polling, see
    main.py).

    A server crash must not take the bot down with it: the Telegram side is
    the main product, and only people who actually use MCP would notice it
    falling over with a port error.

    An unrecoverable configuration error (building the app) isn't worth
    retrying — MCP_PUBLIC_URL won't fix itself between attempts, and an
    instant exception loop would only flood the log. But a crash of an
    already-running server.serve() (say, a transient port issue) used to
    silently leave /mcp dead until the next redeploy — the bot kept answering
    on Telegram the whole time, so there was nothing to notice the outage by.
    This uses the same retry-loop shape as admin_tasks.run_oauth_purge_job.
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
    while True:
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
            return  # чистая остановка (например, отмена задачи) — не падение
        except Exception:
            logger.exception("MCP server crashed, restarting in %ss", MCP_RESTART_DELAY_SECONDS)
        await asyncio.sleep(MCP_RESTART_DELAY_SECONDS)
