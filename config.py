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

# e1RM formula: "epley" or "brzycki"
DEFAULT_E1RM_FORMULA = os.getenv("E1RM_FORMULA", "epley")

# How many sessions to keep visible in the progress screen by default.
PROGRESS_HISTORY_LIMIT = 8

# Hours after which an abandoned active workout triggers a prompt to finish/discard.
STALE_WORKOUT_HOURS = 6

# Number of recent exercises to show first when picking from a muscle group.
RECENT_EXERCISES_LIMIT = 12

# Engagement pushes (streaks, skip reminders, plateau nudges, weekly digest — see
# PUSH_IDEAS.md). Off by default so a fresh deploy doesn't start messaging users
# until this has been reviewed.
ENGAGEMENT_ENABLED = os.getenv("ENGAGEMENT_ENABLED", "false").lower() == "true"

# Local hour (0-23) at which the daily engagement job evaluates and sends pushes.
ENGAGEMENT_HOUR = int(os.getenv("ENGAGEMENT_HOUR", "19"))

# How often (minutes) the post-workout followup job checks for due reminders.
FOLLOWUP_POLL_MINUTES = int(os.getenv("FOLLOWUP_POLL_MINUTES", "10"))

# AI trainer (Grok-backed Q&A over the user's own training data). Same xAI
# key/env names as fun_bot, so one key serves both bots. The menu entry stays
# visible but answers with a hint until the key is set.
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-1-fast")
GROK_BASE_URL = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")

# Delay after finishing a workout before the hydration/protein followup push fires.
FOLLOWUP_DELAY_HOURS = int(os.getenv("FOLLOWUP_DELAY_HOURS", "2"))
