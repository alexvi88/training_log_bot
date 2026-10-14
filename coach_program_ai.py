"""🤖 Программа для атлета от AI-тренера — по просьбе живого тренера.

Вход — «Назначить программу» у тренера (бот: `handlers/coach.py`, приложение:
`POST /v1/coach/clients/{id}/programs/ai`). Чат AI-тренера тут не годится: все
его инструменты читают дневник ТОГО, КТО СПРАШИВАЕТ (`ai_trainer.execute_tool`
получает user_id спрашивающего), и программа «для атлета» собиралась бы по
истории самого тренера. Поэтому здесь один нестримящий вызов, у которого
данные — атлета, а платит и получает результат — тренер:

- связка тренер → атлет проверяется до любого чтения (`not_linked`, как у
  остальных ручек тренера), и атлет должен разрешить передачу своих данных AI
  (`ai_trainer._background_ai_allowed` — то же правило, что у AI-сводки);
- в модель уходит только то, что тренер и так видит на экранах: тренировки с
  подходами и заметками, упражнения, рекорды, назначенные программы, вес тела —
  только если атлет его открыл. Тренировочного профиля атлета (цель, травмы),
  который тот наговорил своему AI-тренеру, тренер не видит — и модель от его
  имени тоже: цель и ограничения тренер пишет сам в `note`;
- лимит вопроса и цена — тренера (`ai_trainer.paid_call(coach_id, KIND_QUESTION)`);
- инструмент один — `propose_program`, и вызов его обязателен; черновик
  разбирается тем же `_propose_program`, что в чате, и сразу ложится тренеру
  новой программой (`ai_program_actions.save_as_new_program`), готовой к
  назначению. Заменять программы тренера этот путь не умеет нарочно.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import ai_limits
import ai_program_actions
import ai_trainer
import coach_service
import config
import db
import i18n
import timeutil

logger = logging.getLogger(__name__)

DAYS_MIN = 1
DAYS_MAX = 6
NOTE_LIMIT = 500
# Сколько последних тренировок атлета (с подходами) уходит в контекст.
RECENT_WORKOUTS = 8
# Упражнений атлета — самых используемых.
EXERCISES_LIMIT = 60


class BuildError(Exception):
    """`code` — машинный: not_linked, client_no_ai_consent, budget,
    program_failed. `message` — готовый текст, если он есть (лимит программ)."""

    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.message = message


def clean_note(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    note = raw.strip()[:NOTE_LIMIT]
    return note or None


async def client_context(coach_id: int, client: Any, link: Any) -> dict[str, Any]:
    """Данные атлета для модели — только то, что тренер видит на экранах.
    Без `profile` и без веса тела, если атлет его не открыл."""
    client_id = client["telegram_id"]
    exercises = await db.list_user_exercises(client_id, limit=EXERCISES_LIMIT)
    data: dict[str, Any] = {
        "athlete_name": link["client_name"],
        "unit": client["unit"] or "kg",
        "summary": await coach_service._client_brief(coach_id, client, link),
        "exercises": [
            {"name": name, "times_used": ex["usage_count"]}
            for ex, name in zip(
                exercises, coach_service.shown_exercise_names([ex["display_name"] for ex in exercises]),
                strict=True,
            )
        ],
        "recent_workouts": (await ai_trainer._recent_workouts(client_id, RECENT_WORKOUTS))["workouts"],
        # Инструмента list_exercise_catalog тут нет — каталог едет сразу:
        # propose_program узнаёт упражнения по его названиям.
        "exercise_catalog": ai_trainer._localized_catalog(i18n.get_lang()),
    }
    if link["show_bodyweight"]:
        latest = await db.get_latest_bodyweight(client_id)
        if latest is not None:
            data["latest_bodyweight"] = {"weight": latest["weight"], "date": latest["logged_at"][:10]}
    return data


def _propose_program_tool() -> dict[str, Any]:
    return next(t for t in ai_trainer.TOOLS if t["function"]["name"] == "propose_program")


async def build_messages(
    coach_id: int, client: Any, link: Any, *, days_per_week: int, note: Optional[str],
) -> list[dict[str, Any]]:
    coach = await db.get_user(coach_id)
    today = timeutil.user_today(coach).isoformat() if coach is not None else ""
    context = await client_context(coach_id, client, link)
    request = i18n.t(
        "coach.program_ai.request",
        name=link["client_name"], days=days_per_week,
        note=note or i18n.t("coach.program_ai.no_note"),
    )
    return [
        # Тот же системный промпт, что у чата: в нём правила сборки программы
        # (объём, схемы, прогрессия) и он же — кэшированный префикс у xAI.
        {"role": "system", "content": ai_trainer._system_prompt()},
        {"role": "user", "content": "\n\n".join([
            request,
            i18n.t("coach.ai.data.today", date=today),
            json.dumps(context, ensure_ascii=False),
        ])},
    ]


async def _model_program_args(coach_id: int, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один нестримящий платный вызов с обязательным propose_program — через
    ai_trainer.paid_call (лимит вопроса ТРЕНЕРА → вызов → цена в лог).
    Возвращает аргументы каждого вызова propose_program. LimitBlocked летит
    наружу."""
    api = ai_trainer._get_client()
    response = await ai_trainer.paid_call(
        coach_id, ai_limits.KIND_QUESTION,
        lambda: api.chat.completions.create(
            model=config.GROK_MODEL,
            max_tokens=4096,
            extra_body={"reasoning_effort": config.GROK_REASONING_EFFORT},
            extra_headers=ai_trainer._cache_headers(coach_id, scope="coach_program"),
            messages=messages,
            tools=[_propose_program_tool()],
            tool_choice={"type": "function", "function": {"name": "propose_program"}},
        ),
        messages=messages,
    )
    await db.try_increment_ai_question_count(coach_id, config.AI_QUESTION_DAILY_LIMIT)
    found = []
    for call in getattr(response.choices[0].message, "tool_calls", None) or []:
        if call.function.name != "propose_program":
            continue
        try:
            args = json.loads(call.function.arguments or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(args, dict):
            found.append(args)
    return found


async def build_for_client(
    coach_id: int, client_id: int, *, days_per_week: int, note: Optional[str] = None,
) -> dict[str, Any]:
    """Собрать программу атлету и положить её тренеру. Возвращает
    `{"program_id", "name", "day_count"}`. `ai_trainer.LimitBlocked` летит
    наружу как есть — его текст показывают обе стороны."""
    link = await db.get_coach_link(coach_id, client_id)
    client = await db.get_user(client_id) if link is not None else None
    if link is None or client is None:
        raise BuildError("not_linked")
    if not await ai_trainer._background_ai_allowed(client):
        raise BuildError("client_no_ai_consent")
    days_per_week = max(DAYS_MIN, min(DAYS_MAX, int(days_per_week)))
    over_budget = await db.routine_budget(coach_id, days_per_week)
    if over_budget:
        raise BuildError("budget", over_budget)

    messages = await build_messages(coach_id, client, link, days_per_week=days_per_week, note=note)
    draft = None
    for args in await _model_program_args(coach_id, messages):
        # Этот путь заводит новую программу и никогда не правит старые.
        args.pop("replaces_program", None)
        _payload, draft = await ai_trainer._propose_program(coach_id, args)
        if draft is not None:
            break
    if draft is None or not draft.get("days"):
        logger.warning("coach program ai: no usable draft for coach %s", coach_id)
        raise BuildError("program_failed")
    draft["replaces"] = None
    draft["name"] = await db.unique_program_name(coach_id, draft["name"])
    saved = await ai_program_actions.save_as_new_program(coach_id, draft)
    if saved.get("error") == "budget":
        raise BuildError("budget", saved.get("message"))
    if saved.get("error"):
        raise BuildError("program_failed")
    return {"program_id": saved["program_id"], "name": saved["name"], "day_count": saved["day_count"]}
