import os

BOT_TOKEN = os.getenv("TG_TOKEN", "")

DB_PATH = os.getenv("DB_PATH", "training_log.db")

DEFAULT_UNIT = "kg"

# e1RM formula: "epley" or "brzycki"
DEFAULT_E1RM_FORMULA = os.getenv("E1RM_FORMULA", "epley")

# How many sessions to keep visible in the progress screen by default.
PROGRESS_HISTORY_LIMIT = 8

# Hours after which an abandoned active workout triggers a prompt to finish/discard.
STALE_WORKOUT_HOURS = 6

# Number of recent exercises to show first when picking from a muscle group.
RECENT_EXERCISES_LIMIT = 8
