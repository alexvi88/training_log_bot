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

# Разовые релизные рассылки (announcements.py) — отдельный тумблер от
# ежедневных пушей: тот выключает реакцию на сигналы в дневнике, а этот —
# анонс новой фичи, который уходит один раз всем. Выключать имеет смысл на
# тестовом развороте, чтобы копия базы не разослала релиз повторно.
ANNOUNCEMENTS_ENABLED = os.getenv("ANNOUNCEMENTS_ENABLED", "true").lower() == "true"

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
# question is allowed web/X search access.
#
# ТЕПЕРЬ grok-4.5, а не grok-4.20-multi-agent — и это не «оптимизация по
# свежести модели», от которой предупреждал прежний комментарий здесь, а вывод из
# первого живого поиска. Он показал, ЗА ЧТО платили:
#
#     multi-agent: 85217 входных токенов, 5438 размышлений      $0.099
#     18 вызовов инструментов (14 web_search + 4 x_search)      $0.090
#
# Multi-agent дешевле грока за токен — это правда. Но он не «ищет дешевле», он
# запускает ЧЕТЫРЕ независимых агента, каждый ищет сколько захочет, и биллятся
# токены всех сразу. Дешевле четырёх не бывает: SDK принимает только 4 или 16.
# То есть желание прежнего комментария (два агента ради экономии) невыполнимо в
# принципе, и крутить agent_count смысла нет.
#
# Наш шаг — «сходи в сеть и перескажи», а не параллельный research по десятку
# источников. Одному агенту grok-4.5 с теми же web_search/x_search это по силам,
# а фан-аута вчетверо не будет ни по токенам, ни по вызовам инструментов.
#
# Если однажды шаг действительно станет research'ем — возвращать multi-agent
# осмысленно, но тогда и лимит AI_SEARCH_DAILY_LIMIT надо считать заново.
GROK_SEARCH_MODEL = os.getenv("GROK_SEARCH_MODEL", "grok-4.5-latest")

# Parallel sub-agent count for the search step (xAI SDK's native agent_count
# param — 4 or 16). Explicit and low: this step is one linear web/X lookup per
# question (see _web_search_findings docstring), not multi-source research, so
# it doesn't need 16 agents — and leaving this unset risks defaulting to the
# most expensive tier (xAI's docs map an unset/high reasoning effort to 16
# agents; all agents' tokens, including their own reasoning and tool calls,
# get billed).
# ЧЕТЫРЕ, а не два. Двойка стояла тут ради экономии — multi-agent самая дорогая
# модель у бота, и биллятся токены всех агентов сразу, — но SDK принимает только
# 4 или 16 (xai_sdk.chat.AgentCountMap) и на двойке падает ValueError ещё в
# chat.create, ДО запроса. То есть живой поиск не работал вовсе: каждая попытка
# умирала на входе, а в логе это выглядело безобидным «ran but found nothing».
# Экономия обернулась молча выключенной функцией.
#
# Допустимые значения зашиты здесь, чтобы кривое значение из окружения снова не
# выключило поиск тихо: неподходящее заменяется ближайшим разрешённым с
# предупреждением в лог. Настоящий рычаг экономии тут не число агентов, а гейт,
# который решает, поднимать ли поиск вообще (см. ai_trainer._gate_verdict).
_ALLOWED_AGENT_COUNTS = (4, 16)
GROK_SEARCH_AGENT_COUNT = int(os.getenv("GROK_SEARCH_AGENT_COUNT", "4"))
if GROK_SEARCH_AGENT_COUNT not in _ALLOWED_AGENT_COUNTS:
    _fallback = min(_ALLOWED_AGENT_COUNTS, key=lambda n: abs(n - GROK_SEARCH_AGENT_COUNT))
    print(
        f"⚠️ GROK_SEARCH_AGENT_COUNT={GROK_SEARCH_AGENT_COUNT} не поддерживается "
        f"(допустимы {list(_ALLOWED_AGENT_COUNTS)}); беру {_fallback}, иначе живой "
        "поиск падал бы на каждом запросе",
        flush=True,
    )
    GROK_SEARCH_AGENT_COUNT = _fallback

# Отдельный потолок для шага живого поиска — он один ходит по SDK и один живёт
# по другим часам, чем обычный запрос к модели: multi-agent действительно
# лазает по сети, и полутора минут ему регулярно не хватало (DEADLINE_EXCEEDED
# на 90с общего AI_REQUEST_TIMEOUT_SECONDS). Обрыв тут — худший из исходов:
# ждали всё это время И остались без находок, а следом ещё ждём основной ответ.
# Раз уж решили искать — доводим до конца; сэкономить время можно только не
# начиная (за это отвечает гейт _gate_verdict).
AI_SEARCH_TIMEOUT_SECONDS = float(os.getenv("AI_SEARCH_TIMEOUT_SECONDS", "180"))

# Per-user daily cap on AI-trainer questions answered with web/X search access.
# Guards against runaway search cost; once hit, the AI trainer still answers
# normally (own tools only, no live search) until the next day.
#
# ПЯТЬ, а не сорок. Сорок стояло, пока поиск был сломан неподдерживаемым
# agent_count и не стоил ничего: каждая попытка падала до запроса. Первый живой
# поиск показал настоящую цену вопроса — около $0.23:
#
#     multi-agent   85217 входных токенов          $0.099
#     18 вызовов инструментов (14 web + 4 X)       $0.090
#     ответ тренера                                $0.040
#
# На сорока это до $9 в день на одного человека — втрое больше всего недельного
# бюджета бота за один день одним пользователем. Пять — чтобы возможность
# осталась, а тумбочка не опустела; поднимать осмысленно после того, как
# подешевеет сам шаг (см. GROK_SEARCH_MODEL).
AI_SEARCH_DAILY_LIMIT = int(os.getenv("AI_SEARCH_DAILY_LIMIT", "5"))

# Тот же потолок, но на ВСЕХ пользователей за сутки (по UTC — счёт от провайдера
# живёт по UTC, а не по местному времени атлета).
#
# Личный лимит не защищает от роста аудитории: он умножается на число людей.
# Десять активных атлетов по пять поисков — это пятьдесят поисков, около $10 за
# день, и ни одного сигнала до счёта в конце месяца. Глобальный потолок — единственное
# место, где расход перестаёт зависеть от того, сколько людей пришло.
#
# Десять — это примерно $2 в день в худшем случае. Держим сознательно ниже
# суммы личных квот: упереться в него должно быть событием, которое видно в
# логе, а не нормой. Поднимать — вместе с общим кэшем находок поиска
# (LLM_COSTS.md, идея 1): он делает второй одинаковый вопрос бесплатным, и тогда
# тот же потолок начинает пропускать заметно больше людей.
AI_SEARCH_GLOBAL_DAILY_LIMIT = int(os.getenv("AI_SEARCH_GLOBAL_DAILY_LIMIT", "10"))

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


# --- Разбор техники по видео (Novita, Qwen3-VL — см. video_analysis.py) ------
#
# Grok видео на вход не берёт: в его chat-API есть text, image_url и общий
# файловый аттач, а отдельного видео-типа нет — «Video» в консоли xAI это
# grok-imagine, генерация. Поэтому кадры смотрит Qwen3-VL, а говорит по-прежнему
# Grok (ai_trainer.ask, параметр video_context) — один персонаж на весь продукт.
#
# Novita OpenAI-совместима, поэтому третьего SDK не завелось: тот же пакет
# openai, только другой base_url. Ключ отдельный от XAI_API_KEY и OPENAI_API_KEY.
# Без ключа раздел просто не показывается, как и голосовой ввод без OPENAI_API_KEY.
NOVITA_API_KEY = os.getenv("NOVITA_API_KEY", "")
# С /v1 на конце — именно этот путь в curl-примере Novita
# (api.novita.ai/openai/v1/chat/completions), а SDK дописывает к base_url только
# /chat/completions. Без /v1 запрос ушёл бы мимо эндпоинта.
NOVITA_BASE_URL = os.getenv("NOVITA_BASE_URL", "https://api.novita.ai/openai/v1")
NOVITA_VIDEO_MODEL = os.getenv("NOVITA_VIDEO_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")

# У Novita в примерах стоит temperature=1 — для «опиши, что видно» это слишком
# свободно: работа тут репортёрская, а не сочинительская, и лишняя свобода идёт
# ровно в выдуманные наблюдения (см. фильтры в video_analysis._sanitize).
VIDEO_ANALYSIS_TEMPERATURE = float(os.getenv("VIDEO_ANALYSIS_TEMPERATURE", "0.2"))

# Сколько разборов видео в день на человека. Дороже обычного вопроса не сильно,
# но заливать ролики можно быстрее, чем печатать вопросы, — и каждый ролик ещё и
# качается из Telegram. Считается по календарному дню пользователя, как и
# остальные квоты (db._quota_day).
AI_VIDEO_DAILY_LIMIT = int(os.getenv("AI_VIDEO_DAILY_LIMIT", "10"))

# Тридцать секунд — подход целиком даже с подходом к снаряду и паузами между
# повторами. Двадцати боялись зря: живой прогон показал, что разбор ролика стоит
# $0.0026, то есть 3% чека — остальное берёт основная модель, когда озвучивает
# наблюдения. Токены у видео растут пропорционально длине, но с такой ставки
# лишние десять секунд стоят десятые доли цента.
#
# Выше поднимать смысла нет: ролик всё равно упирается в MAX_VIDEO_BYTES, а
# длинное видео сэмплируется реже — то есть теряет тот самый темп, ради которого
# мы вообще берём видео, а не кадры. Кружок (video_note) до минуты обрежется этим
# же лимитом.
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "30"))

# Потолок Bot API на скачивание файла ботом — 20 МБ, и обойти его можно только
# своим Bot API сервером. Двадцать секунд с телефона это 3–5 МБ, так что лимит
# по длине упирается раньше; этот стоит вторым рубежом от 4K-роликов.
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(20 * 1024 * 1024)))

# Модель отдаёт структуру для другой модели, а не простыню для чтения, поэтому
# потолок низкий. В живом прогоне «оцени технику» без структуры выходило ~1500
# токенов и ехало сорок секунд — тут хватает вдвое меньшего.
VIDEO_ANALYSIS_MAX_TOKENS = int(os.getenv("VIDEO_ANALYSIS_MAX_TOKENS", "1200"))

# Своё время: разбор видео идёт до основного ответа тренера, и человек всё это
# ждёт. Дольше двух минут ждать в зале никто не станет.
VIDEO_ANALYSIS_TIMEOUT_SECONDS = float(os.getenv("VIDEO_ANALYSIS_TIMEOUT_SECONDS", "120"))


# Потолок на историю, которую храним для кэша (см. ai_trainer._trim_wire_history).
# Держим целиком то, что уехало модели, включая tool-вызовы и их результаты: по
# документации xAI кэш живёт ровно на неизменном префиксе, и любая переписанная
# история — промах.
#
# 150 тысяч, а не 60: на 60 обрезка срабатывала уже на втором вопросе подряд
# («34 сообщений → 20»), потому что результаты инструментов идут на килобайты. А
# обрезка — это холодный промах на следующем запросе, и вот его цена по живым
# логам:
#
#     холодный первый раунд   25106 токенов (из кэша 128)     $0.050
#     тёплый после него       30272 токенов (из кэша 29696)   $0.011
#
# То есть держать историю ДЛИННЕЕ дешевле, чем обрезать: девяносто восемь
# процентов её уезжает по кэш-ставке в 15%, а каждый промах платится целиком.
# Выше поднимать не стоит — у xAI при 200K токенов контекста все ставки
# удваиваются, а 150 тысяч символов это примерно сорок тысяч токенов истории
# поверх одиннадцати тысяч шапки: до порога далеко, но запас нужен.
AI_WIRE_HISTORY_MAX_CHARS = int(os.getenv("AI_WIRE_HISTORY_MAX_CHARS", "150000"))


# Сколько символов вопроса показываем дешёвому гейту. Ему нужно понять ТЕМУ, а не
# прочитать задание: вопрос с ответами на опросник программы приезжает на две
# тысячи токенов инструкций, и на таких входах гейт дважды в проде вернул ноль
# выходных токенов — молча съел бюджет и не решил ничего. Плюс это прямая
# экономия: гейт вызывается на каждый вопрос.
AI_GATE_QUESTION_MAX_CHARS = int(os.getenv("AI_GATE_QUESTION_MAX_CHARS", "600"))


def video_analysis_available() -> bool:
    """Показывать ли разбор видео и принимать ли ролики в чате тренера."""
    return bool(NOVITA_API_KEY)

# --- LLM cost accounting for the daily admin report (see db.cost_events / ---
# admin_tasks.py). ai_trainer.py logs a cost_events row per real chat-completion
# call (model + prompt/completion tokens) and per voice transcription; the
# report prices those against the tables below instead of a flat guess. $/1K
# tokens as (input, output), keyed by the exact model name sent to the API.
# Override without a code change via LLM_PRICES_USD_PER_1K_JSON, e.g.
# '{"grok-4.5-latest": [0.002, 0.006]}'.
#
# grok-4.20-multi-agent's rate here is xAI's short-context per-token price;
# per xAI's docs all sub-agents' tokens (reasoning + tool calls included) are
# billed, not just the leader's — if the usage object we log doesn't already
# sum across agents, this underestimates actual spend on that model. Not
# verified either way.
#
# Стоимости в долларах на ответе чата xAI НЕ отдаёт — проверено по прото SDK:
# `cost_usd_ticks` есть только у Batch API (batch_pb2.BatchCostBreakdown,
# EndpointCost), у SamplingUsage такого поля нет. Прежний комментарий здесь
# обещал его «на ответе» и посылал за авторитетной цифрой туда, где её нет.
# Авторитетные источники — консоли: console.x.ai → Usage и Novita → Usage.
#
# Зато SamplingUsage отдаёт разбивку, из которой цену можно собрать точно:
# prompt_text_tokens, cached_prompt_text_tokens, prompt_image_tokens,
# completion_tokens и ОТДЕЛЬНО reasoning_tokens. Последние тарифицируются как
# выход, по дорогой ставке, — поэтому в логе они печатаются отдельной цифрой:
# если провайдер не включает их в completion_tokens, наш расчёт занижает
# стоимость, и увидеть это можно только глазами на живых числах.
LLM_PRICES_USD_PER_1K: dict[str, tuple[float, float]] = {
    # Снята xAI, в коде не используется. Строка оставлена, чтобы старые записи в
    # cost_events считались по своей ставке, а не по дефолтной.
    "grok-4-1-fast": (0.0002, 0.0005),
    "grok-4.20-multi-agent": (0.00125, 0.0025),
    "grok-4.5-latest": (0.002, 0.006),
    # Novita, разбор видео (video_analysis.py). $0.3/$1.5 за 1M по прайсу модели
    # на novita.ai — здесь в $/1K, как и остальные строки таблицы. Видео на входе
    # тарифицируется теми же входными токенами, отдельной ставки за секунду нет.
    "qwen/qwen3-vl-235b-a22b-instruct": (0.0003, 0.0015),
}
try:
    for _model, _price in json.loads(os.getenv("LLM_PRICES_USD_PER_1K_JSON", "{}")).items():
        LLM_PRICES_USD_PER_1K[_model] = (float(_price[0]), float(_price[1]))
except (TypeError, ValueError, IndexError, json.JSONDecodeError):
    pass
# Ставка для модели, которой нет в таблице выше. ПЕССИМИСТИЧНАЯ намеренно — по
# самой дорогой известной строке. Раньше здесь стояли (0.0002, 0.0005), то есть
# ставка самой дешёвой модели (grok-4-1-fast, уже снятой): переключись мы на любую
# новую модель, не добавив её в таблицу, — и лог с дневным отчётом занизили бы
# расход в десять раз, молча. Занижать расход хуже, чем завышать: завышение
# заметно и его идут проверять, занижение выглядит как хорошая новость.
#
# Максимум берём ПОЭЛЕМЕНТНО, а не max() по кортежам: кортежи сравниваются
# лексикографически, и переопределение через LLM_PRICES_USD_PER_1K_JSON могло бы
# подсунуть строку с самым дорогим входом и дешёвым выходом.
DEFAULT_LLM_PRICE_USD_PER_1K: tuple[float, float] = (
    max((p[0] for p in LLM_PRICES_USD_PER_1K.values()), default=0.002),
    max((p[1] for p in LLM_PRICES_USD_PER_1K.values()), default=0.006),
)


# Во сколько раз кэшированный вход дешевле обычного. 0.15 — по прайсу grok-4.5 в
# console.x.ai: вход $2.00 за 1M, кэшированный вход $0.30 за 1M. То есть за
# совпавший префикс платим 15% цены, и на наших запросах это главная экономия:
# постоянная шапка (системный промпт + схемы 27 инструментов, вместе около
# одиннадцати тысяч токенов) уезжает заново в каждом раунде tool-call'ов, и с
# кэшем стоит копейки вместо трёх полных ставок.
#
# ВНИМАНИЕ на будущее: у xAI прайс двухступенчатый — при контексте свыше 200K
# токенов все ставки удваиваются ($4 вход / $0.60 кэш / $12 выход). Таблица ниже
# этого не знает и считает по дешёвой ступени. Пока наши запросы держатся в
# пределах пятнадцати тысяч токенов, разницы нет; если контекст когда-нибудь
# распухнет за 200K, расчёт начнёт занижать ровно вдвое.
CACHED_INPUT_PRICE_MULTIPLIER = float(os.getenv("CACHED_INPUT_PRICE_MULTIPLIER", "0.15"))


def call_price_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    """Цена одного вызова в долларах по таблице выше.

    Одна формула на два потребителя: дневной отчёт (admin_tasks._llm_cost) и
    строка в логе на каждый вызов (db.log_cost_event). Держать её в двух местах —
    верный способ получить два разных ответа на один вопрос.

    Лог с ценой нужен, чтобы смотреть расход сразу после запроса, а не ждать
    ночного отчёта: цена одного разбора видео или тяжёлого вопроса видна в
    момент, когда её ещё можно связать с тем, что происходило.

    cached_tokens — часть prompt_tokens, приехавшая из кэша провайдера: она
    считается по сниженной ставке (CACHED_INPUT_PRICE_MULTIPLIER), а остаток
    входа — по полной.

    reasoning_tokens — внутренние размышления модели. По прайсу xAI
    (docs.x.ai/developers/pricing, «All standard token types are billed»)
    это отдельный billable тип наравне с входом и выходом, и в completion_tokens
    он не входит. Считаем его по ставке выхода: раньше эти токены не считались
    вовсе, то есть расчёт занижал стоимость — ровно в обратную сторону от кэша,
    из-за чего итог выглядел правдоподобным, будучи неверным дважды.

    Чего эта функция не знает: вызовы серверных инструментов xAI (web_search,
    x_search — $5 за 1000 вызовов сверх токенов). Они бывают только на поисковых
    ответах и в usage не приходят, так что учесть их можно лишь отдельным
    счётчиком вызовов.
    """
    inp, out = LLM_PRICES_USD_PER_1K.get(model, DEFAULT_LLM_PRICE_USD_PER_1K)
    # Больше входа, чем было, из кэша приехать не может — но провайдеру верить на
    # слово тут нельзя: отрицательный «остаток» дал бы отрицательную цену.
    cached = max(0, min(cached_tokens, prompt_tokens))
    fresh = prompt_tokens - cached
    return (
        fresh / 1000 * inp
        + cached / 1000 * inp * CACHED_INPUT_PRICE_MULTIPLIER
        + (completion_tokens + max(0, reasoning_tokens)) / 1000 * out
    )

# Вызовы серверных инструментов xAI (web_search, x_search) — $5 за 1000 вызовов
# СВЕРХ токенов, по docs.x.ai/developers/pricing#tools-pricing. В usage по токенам
# их нет, поэтому считаются отдельными событиями (см.
# ai_trainer._log_server_tool_calls). В консоли за неделю это было «136 calls —
# $0.68»: пятнадцать процентов текстового счёта, которых наш учёт не видел вовсе.
SERVER_TOOL_PRICE_USD_PER_CALL = float(os.getenv("SERVER_TOOL_PRICE_USD_PER_CALL", "0.005"))

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
