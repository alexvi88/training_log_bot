import json
import os

BOT_TOKEN = os.getenv("TG_TOKEN", "")

DB_PATH = os.getenv("DB_PATH", "/data/training_log.db")

# FSM state survives restarts by persisting to this file instead of memory.
FSM_STORAGE_PATH = os.getenv("FSM_STORAGE_PATH", "/data/fsm_storage.json")

# Telegram user id that receives the daily stats report + DB backup. Unset disables the job.
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None

# UTC hour (0-23) at which the daily admin report/backup job runs.
# Deployment clock is UTC (see timeutil.py); 7 UTC == 10:00 MSK (UTC+3).
ADMIN_REPORT_HOUR = int(os.getenv("ADMIN_REPORT_HOUR", "7"))

DEFAULT_UNIT = "kg"

# Pounds per kilogram — used to convert every stored weight when a user
# switches units, so history stays physically correct instead of just relabeled.
LB_PER_KG = 2.20462

# e1RM formula: "epley" or "brzycki"
DEFAULT_E1RM_FORMULA = os.getenv("E1RM_FORMULA", "epley")

# How many sessions to keep visible in the progress screen by default.
PROGRESS_HISTORY_LIMIT = 8

# Hours after which an abandoned active workout triggers a prompt to finish/discard.
STALE_WORKOUT_HOURS = 6

# Number of recent exercises to show first when picking from a muscle group.
# One button per row (the name needs reading, unlike the history list's dates),
# so this doubles as the picker's page length — 12 meant 15 rows of keyboard
# once paging/new-exercise/back are added.
RECENT_EXERCISES_LIMIT = 8

# A name longer than this triggers a "are you sure?" confirmation instead of
# creating/renaming outright — guards against a stray message (typed while the
# bot happened to be waiting for an exercise name) silently becoming an exercise.
# No real exercise name comes close to this, but it might genuinely be intended.
MAX_EXERCISE_NAME_LENGTH = 60

# Описание упражнения уезжает в подпись к фото, а у подписи лимит Telegram — 1024
# символа, а не 4096, как у сообщения. Остаток от 1024 забирают имя, группа,
# оснастка, дата и HTML-разметка, поэтому у самого описания бюджет меньше.
# Проверять его надо на вводе: раньше не проверяли нигде, и карточка упражнения с
# фото после длинного описания падала при каждом открытии.
MAX_EXERCISE_DESCRIPTION_LENGTH = 700

# How many training days one user may keep across all their programs. Lives here
# rather than in ai_trainer.py, where it used to: the cap has nothing to do with
# the AI, and while it sat there only the AI-trainer path enforced it — the
# catalog, the importer and "save from a workout" walked straight past, and then
# the AI path started refusing on a total it hadn't created. See db.routine_budget.
MAX_ROUTINES_PER_USER = 30

# Названия программ и дней, которые вводит пользователь. Тот же потолок, по
# которому AI-тренер режет предложенные им имена: длинное имя едет в подпись
# кнопки списка программ и разносит вёрстку экрана, с которого его только и
# можно переименовать обратно.
MAX_PROGRAM_NAME_LENGTH = 48

# Engagement pushes (streaks, skip reminders, plateau nudges, weekly digest — see
# PUSH_IDEAS.md). On by default; set ENGAGEMENT_ENABLED=false in the environment
# to silence the daily job entirely without touching per-user opt-outs.
ENGAGEMENT_ENABLED = os.getenv("ENGAGEMENT_ENABLED", "true").lower() == "true"

# Local hour (0-23) at which the daily engagement job evaluates and sends pushes.
# Час местный, и он применяется только к тем, у кого часовой пояс известен: кто
# пояс не выставлял, получает пуш в час, безопасный для всех поясов аудитории
# (см. engagement.should_send_now — там же почему). Ночное значение здесь ничего
# не разбудит: тихие часы в engagement стоят поверх этой настройки.
ENGAGEMENT_HOUR = int(os.getenv("ENGAGEMENT_HOUR", "19"))

# Use an AI-generated personalized weekly digest (Sundays) in place of the static
# rotation text, when the AI trainer is configured. Falls back to static copy on
# any failure. Set =false to always use the static digest.
AI_WEEKLY_DIGEST_ENABLED = os.getenv("AI_WEEKLY_DIGEST_ENABLED", "true").lower() == "true"




# AI trainer (Grok-backed Q&A over the user's own training data). Same xAI
# key/env names as fun_bot, so one key serves both bots. The menu entry stays
# visible but answers with a hint until the key is set.
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.5-latest")
GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")

# Hard ceiling on a single model call. The OpenAI SDK defaults to 600s, which
# is not a timeout so much as an abandonment: a hung request leaves the user
# watching "🤔 думаю…" for ten minutes, and the placeholder animation keeps
# cycling the whole time, so it doesn't even look broken.
AI_REQUEST_TIMEOUT_SECONDS = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "90"))

# Как часто накопленный текст ответа уходит в черновик во время стриминга
# (см. ai_trainer._completion_round). Не на каждый токен: каждый флаш — это
# запрос sendMessageDraft, а у Telegram свои лимиты на частоту. Мельче шаг —
# живее «печать», но больше запросов; упрёмся в лимит — стриминг молча
# выключится до конца ответа, и текст приедет одним куском.
# В env, чтобы подбирать на живых ответах без релиза.
AI_STREAM_FLUSH_SECONDS = float(os.getenv("AI_STREAM_FLUSH_SECONDS", "0.6"))

# Как часто черновик реально ПЕРЕРИСОВЫВАЕТСЯ на экране. Отдельно от
# AI_STREAM_FLUSH_SECONDS, хотя раньше это было одно значение: копить текст
# почаще дёшево и полезно, а вот перерисовывать пузырь почаще — то, от чего
# рябит в глазах. За 0.6с быстрая модель успевает выдать несколько строк, и
# каждая перерисовка меняла пол-пузыря — на глаз это не «печать», а мигание.
# Отдаём глазу время дочитать до того, как текст поедет дальше.
AI_DRAFT_INTERVAL_SECONDS = float(os.getenv("AI_DRAFT_INTERVAL_SECONDS", "1.5"))

# Reasoning depth for GROK_MODEL calls (low/medium/high — xAI defaults to
# "high" when unset, which reasoning models can't turn off). Split in two
# rather than one flat value for every call:
#
# GROK_REASONING_EFFORT — the main agentic Q&A loop (_completion_round, see
# ask/_ask_plain), where the model reads tool results and gives actual
# coaching advice. Held at medium for a while on the theory that extra
# thinking pays off here; the console says that theory costs real money —
# reasoning tokens bill at the output rate, the priciest kind, and came to
# $0.46 of a $3.01 week. Dropped to low: the tools already hand the model the
# numbers it reasons about, so the thinking it was buying was mostly
# re-deriving what it had been given. Raise it back if answers get shallower.
#
# GROK_QUICK_REASONING_EFFORT — everything else on GROK_MODEL: the workout
# comment, the weekly digest, food-photo analysis, and the search-worth-it
# gate (a 3-token yes/no classification). None of these need open-ended
# reasoning, and none of them stream — unlike the chat loop, the user has
# nothing live to watch while the model thinks, so low keeps that wait (and
# the cost) down where it doesn't buy anything.
GROK_REASONING_EFFORT = os.getenv("GROK_REASONING_EFFORT", "low")
GROK_QUICK_REASONING_EFFORT = os.getenv("GROK_QUICK_REASONING_EFFORT", "low")

# Search-capable model used (via xAI's gRPC SDK, not the REST endpoint) when a
# question is allowed web/X search access — same model name as fun_bot's
# GROK_SEARCH_MODEL. Kept on grok-4.20-multi-agent rather than grok-4.5: per
# xAI's pricing page, multi-agent is cheaper per token than grok-4.5 ($2.50 vs
# $6.00/1M output), has a 1M context window vs 500k, and a 20% batch discount
# grok-4.5 lacks. It's still current (not on xAI's May-2026 retirement list) —
# don't "optimize" this to grok-4.5 on model-recency instinct alone.
GROK_SEARCH_MODEL = os.getenv("GROK_SEARCH_MODEL", "grok-4.20-multi-agent")

# Parallel sub-agent count for the search step (xAI SDK's native agent_count
# param — 4 or 16). Explicit and low: this step is one linear web/X lookup per
# question (see _web_search_findings docstring), not multi-source research, so
# it doesn't need 16 agents — and leaving this unset risks defaulting to the
# most expensive tier (xAI's docs map an unset/high reasoning effort to 16
# agents; all agents' tokens, including their own reasoning and tool calls,
# get billed).
# Два, а не четыре: по консоли xAI multi-agent — самая дорогая модель у бота
# ($1.40 из $3.07 за неделю), и биллятся токены всех агентов сразу. Наш запрос
# — «сходи в сеть и перескажи», а не «прочеши десяток источников параллельно»,
# так что за четвёртым агентом стоит цена, а не находки. Поднять обратно
# осмысленно ровно тогда, когда шаг станет настоящим research'ем.
GROK_SEARCH_AGENT_COUNT = int(os.getenv("GROK_SEARCH_AGENT_COUNT", "2"))

# Отдельный потолок для шага живого поиска — он один ходит по SDK и один живёт
# по другим часам, чем обычный запрос к модели: multi-agent действительно
# лазает по сети, и полутора минут ему регулярно не хватало (DEADLINE_EXCEEDED
# на 90с общего AI_REQUEST_TIMEOUT_SECONDS). Обрыв тут — худший из исходов:
# ждали всё это время И остались без находок, а следом ещё ждём основной ответ.
# Раз уж решили искать — доводим до конца; сэкономить время можно только не
# начиная (за это отвечает гейт _search_worth_it).
AI_SEARCH_TIMEOUT_SECONDS = float(os.getenv("AI_SEARCH_TIMEOUT_SECONDS", "180"))

# Per-user daily cap on AI-trainer questions answered with web/X search access.
# Guards against runaway search cost; once hit, the AI trainer still answers
# normally (own tools only, no live search) until the next day.
AI_SEARCH_DAILY_LIMIT = int(os.getenv("AI_SEARCH_DAILY_LIMIT", "40"))

# Soft per-user daily cap on AI-trainer questions overall (any kind). Guards
# against a single user running up unbounded model cost; when hit, the trainer
# politely defers until the next day. Generous by default.
AI_QUESTION_DAILY_LIMIT = int(os.getenv("AI_QUESTION_DAILY_LIMIT", "50"))


# Voice input for the AI trainer chat: Telegram voice messages get transcribed
# via OpenAI's speech-to-text before being asked to Grok as a normal text
# question. Separate key from XAI_API_KEY since this hits OpenAI's own API,
# not xAI's — the menu works without it, voice messages just get a hint to
# type instead until it's set.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

# --- LLM cost accounting for the daily admin report (see db.cost_events / ---
# admin_tasks.py). ai_trainer.py logs a cost_events row per real chat-completion
# call (model + prompt/completion tokens) and per voice transcription; the
# report prices those against the tables below instead of a flat guess. $/1K
# tokens as (input, output), keyed by the exact model name sent to the API.
# Override without a code change via LLM_PRICES_USD_PER_1K_JSON, e.g.
# '{"grok-4-1-fast": [0.0002, 0.0005]}'.
#
# grok-4.20-multi-agent's rate here is xAI's short-context per-token price;
# per xAI's docs all sub-agents' tokens (reasoning + tool calls included) are
# billed, not just the leader's — if the usage object we log doesn't already
# sum across agents, this underestimates actual spend on that model. Not
# verified either way; xAI's own `cost_in_usd_ticks` on the response would be
# the authoritative number if this ever needs auditing.
LLM_PRICES_USD_PER_1K: dict[str, tuple[float, float]] = {
    "grok-4-1-fast": (0.0002, 0.0005),
    "grok-4.20-multi-agent": (0.00125, 0.0025),
    "grok-4.5-latest": (0.002, 0.006),
}
try:
    for _model, _price in json.loads(os.getenv("LLM_PRICES_USD_PER_1K_JSON", "{}")).items():
        LLM_PRICES_USD_PER_1K[_model] = (float(_price[0]), float(_price[1]))
except (TypeError, ValueError, IndexError, json.JSONDecodeError):
    pass
DEFAULT_LLM_PRICE_USD_PER_1K: tuple[float, float] = (0.0002, 0.0005)

# Flat per-call estimate for voice transcription (OPENAI_TRANSCRIBE_MODEL) — the
# API doesn't return token counts for audio, so this stands in for a real
# per-second price. Override via TRANSCRIPTION_PRICE_USD_PER_CALL.
TRANSCRIPTION_PRICE_USD_PER_CALL = float(os.getenv("TRANSCRIPTION_PRICE_USD_PER_CALL", "0.006"))

# How long db.cost_events rows are kept — only the daily admin report reads
# this table, and only ever one day back.
COST_EVENTS_RETENTION_DAYS = int(os.getenv("COST_EVENTS_RETENTION_DAYS", "90"))

# Сколько живёт визитка-снапшот (db.shared_items) с момента создания. Таблица
# только росла и ничем не чистилась: каждое «📤 Поделиться» — строка навсегда.
# Полгода — с запасом на «переслал в чат, забрали через месяц»; отозвать ссылку
# раньше можно руками (handlers/sharing.share_revoke).
SHARED_ITEMS_RETENTION_DAYS = int(os.getenv("SHARED_ITEMS_RETENTION_DAYS", "180"))

# Сколько живёт лог действий (db.user_events, пишется из activity_log.py). Строка
# на каждое нажатие — самая быстрорастущая таблица в базе, а смотрят в неё всегда
# про недавнее: «что человек делал на этой неделе». Месяца на это хватает.
ACTIVITY_RETENTION_DAYS = int(os.getenv("ACTIVITY_RETENTION_DAYS", "30"))


# --- MCP: доступ к своим данным из внешних AI-клиентов (Claude и т.п.) ------
#
# Бот поднимает MCP-сервер (streamable HTTP, см. mcp_server.py) рядом с
# поллингом, в том же процессе и на порту контейнера — read-only обёртка над
# теми же ридерами, которыми пользуется AI-тренер. Пользователь выпускает себе
# токен в боте (/mcp) и вставляет его в конфиг своего клиента.
#
# Публичный адрес — единственный обязательный параметр: без него некуда
# посылать пользователя, поэтому и весь раздел в боте не показывается.
# Например: https://training-log.example.com (без /mcp на конце — путь
# дописывается сам).
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")

# Порт, на котором слушает MCP-сервер. Совпадает с containerPort в amvera.yaml:
# наружу контейнер отдаёт ровно один порт, а поллингу он не нужен вовсе.
MCP_PORT = int(os.getenv("MCP_PORT", "80"))

# Аварийный выключатель: MCP_ENABLED=false гасит и сервер, и экран в боте,
# не трогая уже выпущенные токены (они просто перестают куда-либо вести).
MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() not in ("false", "0", "no")


def mcp_available() -> bool:
    """Показывать ли раздел MCP и поднимать ли сервер."""
    return MCP_ENABLED and bool(MCP_PUBLIC_URL)
