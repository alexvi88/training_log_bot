import json
import os

BOT_TOKEN = os.getenv("TG_TOKEN", "")

DB_PATH = os.getenv("DB_PATH", "/data/training_log.db")

# FSM state survives restarts by persisting to this file instead of memory.
FSM_STORAGE_PATH = os.getenv("FSM_STORAGE_PATH", "/data/fsm_storage.json")

# Telegram user id that receives the daily stats report + DB backup. Unset disables the job.
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None

# Local hour (0-23) at which the daily admin report/backup job runs.
ADMIN_REPORT_HOUR = int(os.getenv("ADMIN_REPORT_HOUR", "9"))

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

# Engagement pushes (streaks, skip reminders, plateau nudges, weekly digest — see
# PUSH_IDEAS.md). On by default; set ENGAGEMENT_ENABLED=false in the environment
# to silence the daily job entirely without touching per-user opt-outs.
ENGAGEMENT_ENABLED = os.getenv("ENGAGEMENT_ENABLED", "true").lower() == "true"

# Local hour (0-23) at which the daily engagement job evaluates and sends pushes.
ENGAGEMENT_HOUR = int(os.getenv("ENGAGEMENT_HOUR", "19"))

# Use an AI-generated personalized weekly digest (Sundays) in place of the static
# rotation text, when the AI trainer is configured. Falls back to static copy on
# any failure. Set =false to always use the static digest.
AI_WEEKLY_DIGEST_ENABLED = os.getenv("AI_WEEKLY_DIGEST_ENABLED", "true").lower() == "true"


# Sticker reactions (see stickers.py). STICKER_PACKS is a comma-separated list
# of *short names* — the part after t.me/addstickers/ — of packs the bot re-sends
# from. Nothing is bundled: the stickers are fetched from Telegram at runtime, so
# any public pack works and swapping one is an env change. Unset = no stickers
# anywhere, whatever STICKERS_ENABLED says.
#
# To find a pack's short name, send any sticker from it to this bot as the admin
# (ADMIN_ID) — it replies with the name (see handlers/admin.py).
STICKER_PACK_NAMES = [
    name.strip() for name in os.getenv("STICKER_PACKS", "").split(",") if name.strip()
]

# Master switch for sticker reactions, on top of the per-user setting. Set
# STICKERS_ENABLED=false to silence them for everyone without dropping the pack.
STICKERS_ENABLED = os.getenv("STICKERS_ENABLED", "true").lower() == "true"


# AI trainer (Grok-backed Q&A over the user's own training data). Same xAI
# key/env names as fun_bot, so one key serves both bots. The menu entry stays
# visible but answers with a hint until the key is set.
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.5-latest")
GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")

# Reasoning depth for GROK_MODEL calls (low/medium/high — xAI defaults to
# "high" when unset, which reasoning models can't turn off). Medium is the
# documented middle ground for latency-tolerant but not fully open-ended
# tasks, which fits the AI trainer's chat/classification calls.
GROK_REASONING_EFFORT = os.getenv("GROK_REASONING_EFFORT", "medium")

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
GROK_SEARCH_AGENT_COUNT = int(os.getenv("GROK_SEARCH_AGENT_COUNT", "4"))

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
