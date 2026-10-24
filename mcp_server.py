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
* Почти всё чтение. Пишущие инструменты (`WRITE_TOOLS` ниже) выбраны по
  одному критерию: внутри бота они уже «делают сразу и дают откат кнопкой»
  (`ai_trainer._UNDOABLE_TOOLS`), то есть их и так можно вызвать одной
  репликой без подтверждения. Кнопки отката у
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
import contextlib
import json
import logging
import time
from typing import Any, Iterator, Optional

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations

import ai_trainer
import api_v1
import body_limit
import coach_invite_page
import config
import db
import i18n
import legal_pages
import mcp_oauth
import mcp_wellknown
import referral_page
import seo_http
import seo_pages

logger = logging.getLogger(__name__)

# Путь, на котором отвечает MCP. Совпадает с тем, что показывает экран /mcp.
MCP_PATH = mcp_oauth.MCP_PATH

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
        "get_saved_programs",
        "get_program_adherence",
        "get_muscle_recovery",
        "compare_periods",
    }
)

# Пишущие инструменты, разрешённые снаружи, — подмножество
# `ai_trainer._UNDOABLE_TOOLS`, отобранное по двум критериям:
#
# 1. Добавление, а не правка существующего: log_bodyweight,
#    create_exercise, copy_program сами описаны как «ДЕЛАЕТ СРАЗУ — добавление
#    ничего не портит». Новую строку легко найти и убрать вручную, если она
#    лишняя.
# 2. Удаление с полным восстановлением: delete_bodyweight_log удаляет
#    конкретную запись (id — из get_bodyweight_history), но её undo не
#    «намекает вернуться и переделать», а честно воссоздаёт ту же строку с теми
#    же значениями (kind=bodyweight_restore в ai_undo.apply). Это
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
    "log_bodyweight", "create_exercise", "copy_program", "delete_bodyweight_log",
})

EXPOSED_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS

# Публичное имя коннектора — то, что видят в каталогах Claude/ChatGPT и в
# официальном MCP Registry (server.json в корне, docs/MCP_LISTING.md).
SERVER_NAME = "athlete-diary"
SERVER_TITLE = "Athlete Diary"
SERVER_VERSION = "1.1.0"


# Аннотации инструментов (MCP ToolAnnotations). Каталог коннекторов Claude
# требует у каждого инструмента `title` и `readOnlyHint` или `destructiveHint`,
# каталог ChatGPT — явные readOnlyHint, destructiveHint и openWorldHint у всех.
# По ним клиент решает, звать ли без подтверждения: чтение — молча, удаление —
# всегда с вопросом. Поэтому ставим все четыре явно, а не полагаемся на
# умолчания спецификации (там destructiveHint по умолчанию true).
#
# openWorldHint=False у всех: инструменты ходят только в дневник самого атлета,
# ничего не публикуют и не трогают чужое.
def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    )


def _write(title: str, *, destructive: bool, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=destructive,
        idempotent_hint=idempotent, open_world_hint=False,
    )


# Аннотация на каждый выставленный инструмент — по имени, рядом с белыми
# списками: tests/test_mcp_server.py сверяет, что ключи совпадают с
# EXPOSED_TOOLS и что readOnlyHint совпадает с READ_ONLY_TOOLS.
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_training_overview": _read_only("Training overview"),
    "get_active_workout": _read_only("Current workout"),
    "list_recent_workouts": _read_only("Recent workouts"),
    "get_full_workout_history": _read_only("Full workout history"),
    "get_muscle_recovery": _read_only("Muscle recovery"),
    "get_weekly_volume_by_group": _read_only("Weekly volume by muscle group"),
    "get_exercise_progress": _read_only("Exercise progress"),
    "list_exercise_catalog": _read_only("Exercise catalog"),
    "get_bodyweight_history": _read_only("Body weight history"),
    "get_saved_programs": _read_only("Saved programs"),
    "compare_periods": _read_only("Compare two periods"),
    "get_program_adherence": _read_only("Program adherence"),
    # Добавление новой строки: ничего существующего не портит. Повтор
    # log_bodyweight пишет второе взвешивание — не идемпотентно; повтор
    # create_exercise с тем же именем отвечает «уже есть» — идемпотентно;
    # copy_program без new_name каждый раз берёт новое свободное имя.
    "log_bodyweight": _write("Log body weight", destructive=False, idempotent=False),
    "create_exercise": _write("Create custom exercise", destructive=False, idempotent=True),
    "copy_program": _write("Copy a saved program", destructive=False, idempotent=False),
    # Удаление — destructive, клиент спросит человека. Повтор с тем же id
    # ничего не удаляет второй раз — идемпотентно.
    "delete_bodyweight_log": _write("Delete a body weight entry", destructive=True, idempotent=True),
}

# Потолок вызовов инструментов на атлета. Ридеры дешёвые, но
# get_full_workout_history собирает до двухсот тренировок, а модель в цикле
# умеет звать одно и то же десятками раз. Шестьдесят в минуту живой разговор
# не задевает (обычный вопрос — два-пять вызовов), а зациклившийся клиент
# упирается в честную ошибку вместо того, чтобы грузить процесс бота.
# Память процесса, а не база: это предохранитель, не учёт, и рестарт его
# обнуляет без вреда.
TOOL_CALLS_PER_MINUTE = 60
_TOOL_CALL_WINDOW = 60.0
_tool_calls: dict[int, list[float]] = {}


class RateLimited(Exception):
    """Слишком много вызовов подряд. Наружу уходит как ошибка вызова."""


def _allow_call(user_id: int, now: Optional[float] = None) -> bool:
    moment = time.monotonic() if now is None else now
    window_start = moment - _TOOL_CALL_WINDOW
    hits = [t for t in _tool_calls.get(user_id, []) if t > window_start]
    if len(hits) >= TOOL_CALLS_PER_MINUTE:
        _tool_calls[user_id] = hits
        return False
    hits.append(moment)
    _tool_calls[user_id] = hits
    return True


async def _user_lang(user_id: int) -> str:
    user = await db.get_user(user_id)
    return user["lang"] if user is not None else i18n.DEFAULT_LANG

# Инструменту нечего сказать про кнопку, которой здесь нет — см. WRITE_TOOLS.
#
# Без имени конкретного экрана: первая версия говорила «поправить можно в
# 🏋️ Дневник веса» — текст, скопированный с log_bodyweight и подставлявшийся
# ВСЕМ WRITE_TOOLS. create_exercise ответил им же — «поправить упражнение
# можно в Дневнике веса» неправда, у него вообще нет отдельного экрана. Общая
# формулировка верна для всех.
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
    return i18n.t_in(await _user_lang(user_id), "mcp.write_note")


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
        #
        # Путей за новым кодом два, и называем оба: у аккаунта, заведённого
        # через Apple, бота нет — ему «набери /mcp» тупик.
        raise Unauthorized(
            "Access expired. Connect Athlete Diary again: get a new code in the "
            "iPhone app (Profile → Connect to Claude and ChatGPT) or in the "
            "Telegram bot (/mcp).\n"
            "Нужен действующий доступ. Подключи дневник заново: новый код — в "
            "приложении (Профиль → «Подключить к Claude и ChatGPT») или в боте "
            "(/mcp)."
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
    tools `note` IS `_UNDO_NOTE` verbatim (log_bodyweight,
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
    if not _allow_call(user_id):
        logger.info("MCP tool %s for user %s: rate limited", tool_name, user_id)
        raise RateLimited(
            i18n.t_in(await _user_lang(user_id), "mcp.error.rate_limited", n=TOOL_CALLS_PER_MINUTE)
        )
    logger.info("MCP tool %s for user %s", tool_name, user_id)
    # Язык атлета — явно, из базы: MCP-вызов приходит не апдейтом и не через
    # /v1, контекст языка никто не выставил, и без этого каталог упражнений,
    # имена групп и подписи внутри ответа ехали бы на языке по умолчанию
    # (русском) англоязычному атлету.
    with i18n.use_lang(await _user_lang(user_id)):
        raw = await ai_trainer.execute_tool(user_id, tool_name, arguments)
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
    site = config.site_base_url()
    mcp = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        description=(
            "Your own Athlete Diary workout log: history, progress, records, "
            "muscle volume, body weight and programs."
        ),
        instructions=(
            "The signed-in athlete's own Athlete Diary workout log: workout and set "
            "history, exercise progress and e1RM, weekly volume by muscle group, muscle "
            "recovery, body weight, and saved programs. Almost everything is read-only — "
            "except log_bodyweight, create_exercise, copy_program, and "
            "delete_bodyweight_log, which change the diary (renaming and other edits "
            "happen in the app or the bot). Start with get_training_overview — it shows "
            "the units (kg/lb) and the exact exercise names the other tools need. For "
            "long time spans use get_full_workout_history rather than "
            "list_recent_workouts. If get_exercise_progress returns no sessions, the "
            "exercise has no logged entries yet: say so instead of inventing numbers. "
            "Before create_exercise, check the athlete's list and the catalog for the "
            "same movement. Write tools are for explicit requests from the athlete; "
            "ids for delete_bodyweight_log come from get_bodyweight_history."
        ),
        website_url=site,
        icons=(
            [Icon(src=f"{site}/site/icons/icon-512.png", mime_type="image/png", sizes=["512x512"])]
            if site
            else None
        ),
        version=SERVER_VERSION,
        auth_server_provider=mcp_oauth.TrainingLogOAuthProvider(),
        auth=mcp_oauth.auth_settings(MCP_PATH),
    )
    mcp_oauth.register_routes(mcp)
    # /privacy и /terms — публичные страницы для App Store (см. legal_pages.py).
    legal_pages.register_routes(mcp)
    # /coach/<код> — страница приглашения тренера и AASA для Universal Links
    # (см. coach_invite_page.py).
    coach_invite_page.register_routes(mcp)
    # /i/ref_<id> — реферальная ссылка атлета без Telegram (см. referral_page.py).
    referral_page.register_routes(mcp)
    # Публичные страницы для поиска: /ru/..., /en/..., sitemap и robots
    # (см. seo_pages.py).
    seo_pages.register_routes(mcp)
    # Файлы подтверждения домена для каталогов коннекторов (MCP Registry,
    # ChatGPT, Glama) и карточка сервера (см. mcp_wellknown.py).
    mcp_wellknown.register_routes(mcp)

    def tool(name: str):
        """`@mcp.tool` с заголовком и аннотациями из TOOL_ANNOTATIONS — у
        каждого инструмента они обязаны быть, и забыть их тут негде."""
        annotations = TOOL_ANNOTATIONS[name]
        return mcp.tool(name=name, title=annotations.title, annotations=annotations)

    @tool("get_training_overview")
    async def get_training_overview() -> str:
        """Athlete summary: units, e1RM formula, stats (total workouts, this week, last 30
        days, days since last one, streak) and their exercise list with muscle group and
        use count."""
        return await _call("get_training_overview")

    @tool("get_active_workout")
    async def get_active_workout() -> str:
        """The current unfinished workout: when it started, what's logged so far, and
        exercise notes. An empty result means the athlete isn't training right now."""
        return await _call("get_active_workout")

    @tool("list_recent_workouts")
    async def list_recent_workouts(limit: int = 5) -> str:
        """The most recent completed workouts (1-10, default 5): date, notes, and every
        set (weight x reps) for each exercise."""
        return await _call("list_recent_workouts", limit=max(1, min(int(limit), 10)))

    @tool("get_full_workout_history")
    async def get_full_workout_history() -> str:
        """The full workout history (up to the last 200) — for questions about a long
        time span: trends over six months, monthly volume, finding old workouts."""
        return await _call("get_full_workout_history")

    @tool("get_muscle_recovery")
    async def get_muscle_recovery() -> str:
        """How recovered each muscle group is: recovery percentage, when it was last
        trained, and how many days ago. 100% means ready for heavy work, below 85% means
        it hasn't recovered yet."""
        return await _call("get_muscle_recovery")

    @tool("get_weekly_volume_by_group")
    async def get_weekly_volume_by_group() -> str:
        """Volume (working sets) by muscle group for the current and previous week —
        where the load is lopsided and what's falling short."""
        return await _call("get_weekly_volume_by_group")

    @tool("get_exercise_progress")
    async def get_exercise_progress(exercise_name: str) -> str:
        """Progress on a single exercise: sets by date, working weights, e1RM, records.
        exercise_name: an exercise name exactly as listed in the training overview.
        total_sessions 0 (an empty sessions list) means the exercise has no logged
        entries."""
        return await _call("get_exercise_progress", exercise_name=exercise_name)

    @tool("list_exercise_catalog")
    async def list_exercise_catalog() -> str:
        """The built-in exercise catalog by muscle group — what could be suggested
        beyond what the athlete already does."""
        return await _call("list_exercise_catalog")

    @tool("get_bodyweight_history")
    async def get_bodyweight_history() -> str:
        """Weigh-in history: date and body weight."""
        return await _call("get_bodyweight_history")

    @tool("get_saved_programs")
    async def get_saved_programs(name: str = "") -> str:
        """The athlete's saved programs. Without `name` — the list: program names, kind
        (multi-day program or standalone day) and how many days each has. With `name`
        — that one program in full: every day, its exercises, set schemes and
        progression rules. name must match a program name exactly."""
        return await _call("get_saved_programs", name=name.strip() or None)

    @tool("compare_periods")
    async def compare_periods(days: int = 90) -> str:
        """What changed across all exercises: the last N days against the previous N
        (default 90) — workouts, sets, best e1RM, and tonnage in each window plus the
        difference, and separately, exercises that were dropped or newly started."""
        return await _call("compare_periods", days=max(7, min(int(days), 365)))

    @tool("get_program_adherence")
    async def get_program_adherence() -> str:
        """How closely actual workouts match the saved program: what's being done on
        plan, what's being skipped, what's been added on top."""
        return await _call("get_program_adherence")

    @tool("log_bodyweight")
    async def log_bodyweight(weight: float) -> str:
        """Log a body weight entry for today. weight is in the athlete's own unit
        (kg or lb, as set in their diary); no conversion is done."""
        # The external client called this tool on purpose — that IS the
        # explicit request the in-bot guard (user_asked_to_log) asks for.
        return await _call("log_bodyweight", weight=weight, user_asked_to_log=True)

    @tool("create_exercise")
    async def create_exercise(name: str, group: str) -> str:
        """Add a custom exercise that's in neither the athlete's list nor the catalog —
        adding never changes existing data. group: a muscle group name exactly as listed
        in the athlete's exercise list or the catalog."""
        return await _call("create_exercise", name=name, group=group)

    @tool("copy_program")
    async def copy_program(name: str, new_name: str | None = None) -> str:
        """Duplicate a saved program with all its days, exercises, and set schemes —
        the original stays untouched. name: the source program's exact name. Without
        new_name, a free name near the original is picked automatically ("PPL (2)")."""
        return await _call("copy_program", name=name, new_name=new_name)

    @tool("delete_bodyweight_log")
    async def delete_bodyweight_log(log_id: int) -> str:
        """Delete one body weight entry by its id, as shown in the weigh-in history."""
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
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # SDK's dynamic client registration (RFC 7591) caps only metadata size
    # (see TrainingLogOAuthProvider.register_client / db.OAUTH_CLIENT_METADATA_LIMIT),
    # not request frequency — an anonymous flood can grow oauth_clients for hours
    # before the once-a-day prune catches up.
    mcp_app = mcp_oauth.RegisterRateLimitMiddleware(mcp_app)
    # /v1 — REST API for the iOS client (api_v1.py), on the same port: there's
    # already one uvicorn running here for MCP + OAuth, and a second process
    # would need its own port and its own restart-on-crash story for free.
    #
    # Not a Starlette Mount: Router.lifespan() only runs Starlette's own
    # lifespan_context and never forwards "lifespan" scope events into a
    # mounted sub-app, so wrapping mcp_app in Mount("/", ...) would silently
    # skip the startup that sets up its streamable-http session manager (the
    # first request would then fail with "Task group is not initialized").
    # A plain path-prefix dispatcher sidesteps that: lifespan always goes to
    # mcp_app, the only one of the two with startup/shutdown state.
    app = _PrefixDispatch(prefix="/v1", prefix_app=api_v1.build_app(), default_app=mcp_app)
    # Публичный сайт: 301 на свой домен, gzip и ETag/304 — только для адресов
    # сайта (seo_http.py); /v1, OAuth, /mcp и остальное проходит мимо как было.
    app = seo_http.SiteMiddleware(app)
    # Общий потолок на тело запроса — снаружи ОБОИХ приложений (см.
    # body_limit.py про то, почему ни Starlette/uvicorn, ни amvera.yaml его
    # не ставят сами): один процесс отвечает и за бота, и за весь этот порт,
    # и слишком большое тело не должно валить его целиком, независимо от
    # того, в /v1 оно пришло или в /mcp. Middleware пропускает lifespan-
    # события мимо себя без изменений (см. её докстринг), так что порядок
    # оборачивания здесь не важен для запуска/остановки mcp_app.
    return body_limit.MaxBodySizeMiddleware(app, config.MAX_REQUEST_BODY_BYTES)


class _PrefixDispatch:
    """Route by path prefix, and send lifespan only to `default_app`.

    See build_app() for why this exists instead of starlette.routing.Mount.
    """

    def __init__(self, *, prefix: str, prefix_app, default_app):
        self._prefix = prefix
        self._prefix_app = prefix_app
        self._default_app = default_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._default_app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == self._prefix or path.startswith(self._prefix + "/"):
            scope = dict(scope)
            scope["path"] = path[len(self._prefix):] or "/"
            scope["root_path"] = scope.get("root_path", "") + self._prefix
            await self._prefix_app(scope, receive, send)
            return
        await self._default_app(scope, receive, send)


class _Server(uvicorn.Server):
    """uvicorn без своих обработчиков сигналов: остановкой процесса командует
    main.py (shutdown_drain.py). Свой `capture_signals` перехватывал SIGINT/
    SIGTERM поверх наших и гасил HTTP за 0.1 с — вместе со стримами /v1/ai/ask,
    которые ещё писались."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


_server: Optional[_Server] = None
_serve_task: Optional[asyncio.Task] = None
_stopping = False


async def stop(timeout: float) -> None:
    """Мягко погасить HTTP: перестать принимать соединения, дать идущим
    запросам до `timeout` секунд (uvicorn `timeout_graceful_shutdown`) и
    дождаться выхода `serve()`. Зовёт shutdown_drain.shutdown — уже ПОСЛЕ того,
    как ходы AI-тренера договорили."""
    global _stopping
    _stopping = True
    server, task = _server, _serve_task
    if server is not None:
        server.config.timeout_graceful_shutdown = timeout
        server.should_exit = True
    if task is None or task.done():
        return
    # Запас сверх таймаута uvicorn — на lifespan shutdown; не уложился — рвём.
    _done, pending = await asyncio.wait({task}, timeout=timeout + 2)
    for leftover in pending:
        leftover.cancel()


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
    global _server, _serve_task
    _serve_task = asyncio.current_task()
    while not _stopping:
        server = _server = _Server(
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
