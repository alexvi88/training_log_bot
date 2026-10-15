"""🤖 Программа атлету от AI-тренера — в обычном чате тренера, режимом «для атлета».

Вход — «Назначить программу» → «Составить с AI-тренером» у тренера (бот:
`handlers/coach.py` → `handlers/ai_trainer.start_client_program`, приложение:
`POST /v1/ai/conversations/coach-client`). Дальше всё как у атлета, который
собирает программу себе: тот же чат, тот же опросник `ask_setup_questions`,
тот же `propose_program` и та же карточка с «Забрать». Отличие одно — чей
дневник читают инструменты. Его и держит `ClientChat`, который
`ai_trainer.ask(..., subject=...)` получает вместо обычного набора:

- связка тренер → атлет и согласие атлета на AI проверяются до любого чтения
  и заново на каждом ходе (`open_chat`): `not_linked` — как у остальных ручек
  тренера, `client_no_ai_consent` — то же правило, что у AI-сводки
  (`ai_trainer._background_ai_allowed`);
- инструменты чтения отдают дневник АТЛЕТА и только то, что тренер и так
  видит на экранах: тренировки с подходами и заметками, упражнения, рекорды,
  назначенные программы, вес тела — только если атлет его открыл. Профиля
  атлета (цель, травмы), который тот наговорил своему AI-тренеру, тренер не
  видит — и модель от его имени тоже: поле `profile` из обзора вырезано, а
  цель и ограничения модель спрашивает у тренера опросником;
- ничего не пишет: ни в дневник атлета, ни в профиль тренера —
  `save_athlete_profile`, записи веса, еды, упражнений и правки программ в
  этом режиме модели не показываются вовсе (`CLIENT_TOOL_NAMES`);
- `propose_program` — тот же разбор, что в чате, но от имени тренера:
  черновик ложится тренеру НОВОЙ программой (`replaces_program` срезан) и
  несёт `for_client` — по нему карточка после «Забрать» предлагает сразу
  назначить её этому атлету;
- лимит вопроса и цена — тренера: ход идёт обычным путём чата под его id;
- язык: с тренером модель говорит на ЕГО языке (язык хода), а всё, что
  делается для атлета, — на языке АТЛЕТА (`users.lang`): каталог упражнений
  (`list_exercise_catalog`), имена его упражнений в обзоре, а в черновике —
  каталожные упражнения и схемы подходов (`_localize_draft`, по
  идентичности каталога); имя программы, дней и описание модель пишет на
  языке атлета по заметке режима (`coach.ai_chat.mode_lang.*`). Копия у
  атлета после «Назначить» — тоже на его языке (coach_service.assign_program).

Кэш: режим — отдельный разговор со своим префиксом (заметка режима вторым
системным сообщением, свой набор инструментов, свой conv-id `coach_client`).
Свой разговор тренера при входе не переписывается и не архивируется
(`db.start_coach_client_conversation`, в боте — заначка в FSM), «Выйти»
возвращает его байт-в-байт.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import ai_trainer
import coach_service
import db
import i18n
import seed_data

logger = logging.getLogger(__name__)

# Инструменты, которые модель видит в режиме «программа атлету». Порядок схем
# берётся из ai_trainer.TOOLS (он — часть кэшируемого префикса). Чтение — по
# дневнику атлета, опросник и propose_program — тренеру.
CLIENT_TOOL_NAMES = frozenset({
    "get_training_overview",
    "list_recent_workouts",
    "get_full_workout_history",
    "get_muscle_recovery",
    "get_weekly_volume_by_group",
    "get_exercise_progress",
    "list_exercise_catalog",
    "get_bodyweight_history",
    "ask_setup_questions",
    "propose_program",
    "compare_periods",
    "get_stalled_lifts",
})

# Читают дневник атлета через общий ai_trainer.execute_tool — ни один из них
# ничего не пишет.
_CLIENT_READ_TOOLS = frozenset({
    "list_recent_workouts",
    "get_full_workout_history",
    "get_muscle_recovery",
    "get_weekly_volume_by_group",
    "get_exercise_progress",
    "list_exercise_catalog",
    "compare_periods",
    "get_stalled_lifts",
})


class ModeError(Exception):
    """`code` — машинный: not_linked, client_no_ai_consent."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def client_tools() -> list[dict[str, Any]]:
    return [t for t in ai_trainer.TOOLS if t["function"]["name"] in CLIENT_TOOL_NAMES]


@dataclass(frozen=True)
class ClientChat:
    """Субъект хода чата тренера: дневник `client_id`, платит `coach_id`."""

    coach_id: int
    client_id: int
    name: str
    unit: str
    show_bodyweight: bool
    # Язык атлета (users.lang) — не строка текста, а код: на нём рендерится
    # то, что делается для атлета (см. модульную докстрингу).
    lang: str = i18n.DEFAULT_LANG

    cache_scope = "coach_client"

    @property
    def tools(self) -> list[dict[str, Any]]:
        return client_tools()

    def system_note(self) -> str:
        """Второе системное сообщение разговора — одно и то же на каждом ходе
        (имя, единица и язык атлета посреди разговора не меняются), поэтому
        префикс кэша не рвёт.

        Атлет на другом языке, чем тренер, — строка о том, на каком языке
        писать программу: с тренером модель говорит на его языке (языковой
        хвост системного промпта), а имя программы, дней и описание читает
        атлет."""
        note = i18n.t("coach.ai_chat.mode_prompt", name=self.name, unit=self.unit)
        if self.lang != i18n.get_lang():
            note = f"{note} {i18n.t(f'coach.ai_chat.mode_lang.{self.lang}')}"
        return note

    def as_json(self) -> dict[str, Any]:
        return {"client_id": self.client_id, "name": self.name}

    async def execute(
        self,
        name: str,
        tool_input: dict[str, Any],
        on_program: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
        on_questions: Optional[Callable[[list[dict[str, Any]]], Awaitable[None]]] = None,
    ) -> str:
        if name not in CLIENT_TOOL_NAMES:
            payload: dict[str, Any] = {
                "error": f"{name} is not available while building a program for the athlete"
            }
        elif name == "get_training_overview":
            payload = await self._overview()
        elif name == "list_exercise_catalog":
            # Каталог — на языке атлета: модель берёт имена отсюда, и
            # программа атлету называет упражнения его словами. Обратно
            # имя резолвится на любом языке (db._find_global_template_by_name).
            payload = {"catalog": ai_trainer._localized_catalog(self.lang)}
        elif name == "get_bodyweight_history":
            if self.show_bodyweight:
                payload = await ai_trainer._bodyweight_history(self.client_id)
                for entry in payload.get("entries") or []:
                    entry.pop("id", None)
            else:
                payload = {
                    "error": "the athlete hasn't shared body weight with the coach — build without it",
                }
        elif name == "ask_setup_questions":
            payload, questions = await ai_trainer._ask_setup_questions(self.coach_id, tool_input)
            if questions is not None and on_questions is not None:
                await on_questions(questions)
                ai_trainer.sync_setup_payload(payload, questions)
        elif name == "propose_program":
            # Только новая программа тренера: правка его сохранённых — не этот
            # разговор, а программы атлета модель отсюда не трогает вовсе.
            tool_input = dict(tool_input)
            tool_input.pop("replaces_program", None)
            payload, draft = await ai_trainer._propose_program(self.coach_id, tool_input)
            if draft is not None:
                await self._localize_draft(draft)
                draft["replaces"] = None
                draft["for_client"] = self.as_json()
                payload["note"] = i18n.t("coach.ai_chat.proposed_note")
                if on_program is not None:
                    await on_program(draft)
        else:
            return await ai_trainer.execute_tool(self.client_id, name, tool_input)
        return json.dumps(payload, ensure_ascii=False)

    async def _localize_draft(self, draft: dict[str, Any]) -> None:
        """Черновик — на языке атлета: каталожное упражнение по идентичности
        (`original_name` у своего упражнения тренера, имя шаблона у ещё не
        взятого) — именем каталога на языке атлета, если тренер его не
        переименовал; своё имя тренера — как есть. Схема подходов («3 sets»,
        «30–60 sec») — тоже на языке атлета.

        Сохраняется черновик тренеру его же упражнениями: «Жим штанги лёжа»
        у англоязычного тренера резолвится по той же идентичности в его
        «Barbell Bench Press» (db.get_or_create_user_exercise_by_name), а
        назначенная копия у атлета — в его форк на его языке."""
        for day in draft.get("days") or []:
            for item in day.get("items") or []:
                _source, shown, identity = await db.resolve_exercise_identity(self.coach_id, item["name"])
                if identity is not None and shown in {
                    seed_data.localized_exercise_name(identity, lang) for lang in i18n.SUPPORTED
                }:
                    item["name"] = seed_data.localized_exercise_name(identity, self.lang)
                item["target"] = seed_data.relocalized_target(item.get("target"), self.lang)

    async def _overview(self) -> dict[str, Any]:
        """Обзор дневника атлета без того, чего тренер не видит: профиля
        (цель, опыт, травмы) и веса тела, если атлет его не открыл. Плюс то,
        что тренер видит на карточке клиента: рекорды и назначенные им
        программы (`coach_service._client_brief`)."""
        overview = await ai_trainer._training_overview(self.client_id)
        overview.pop("profile", None)
        if not self.show_bodyweight:
            overview.pop("latest_bodyweight", None)
        link = await db.get_coach_link(self.coach_id, self.client_id)
        client = await db.get_user(self.client_id)
        # Имена упражнений атлета — на его языке, как и каталог в этом режиме.
        exercises = overview.get("exercises") or []
        shown = coach_service.shown_exercise_names(client, [ex["name"] for ex in exercises])
        for ex, shown_name in zip(exercises, shown, strict=True):
            ex["name"] = shown_name
        if link is not None and client is not None:
            overview["coach_view"] = await coach_service._client_brief(self.coach_id, client, link)
        overview["athlete_name"] = self.name
        return overview


async def open_chat(coach_id: int, client_id: int) -> ClientChat:
    """Проверить связку и согласие атлета — до любого чтения его дневника.
    `ModeError` с кодом, если нельзя."""
    link = await db.get_coach_link(coach_id, client_id)
    client = await db.get_user(client_id) if link is not None else None
    if link is None or client is None:
        raise ModeError("not_linked")
    if not await ai_trainer._background_ai_allowed(client):
        raise ModeError("client_no_ai_consent")
    return ClientChat(
        coach_id=coach_id,
        client_id=client_id,
        name=link["client_name"],
        unit=client["unit"] or "kg",
        show_bodyweight=bool(link["show_bodyweight"]),
        lang=coach_service.client_lang(client),
    )


# ---------- тексты сценария ----------


def seed(name: str) -> str:
    """Просьба модели собрать программу атлету — то же, что
    `ai.screen.build_program_seed`, только про атлета."""
    return i18n.t("coach.ai_chat.seed", name=name)


def shown_seed(name: str) -> str:
    """Своя реплика тренера в ленте вместо служебного seed."""
    return i18n.t("coach.ai_chat.shown_seed", name=name)


def is_seed(text: str) -> bool:
    """Это просьба собрать программу — обычная кнопка «Составь мне программу»
    (приложение шлёт её и в режиме атлета) или seed режима, на любом языке."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    for lang in i18n.SUPPORTED:
        if stripped == i18n.t_in(lang, "ai.screen.build_program_seed").strip():
            return True
        marker = i18n.t_in(lang, "coach.ai_chat.seed", name="\0").split("\0")[0]
        if marker and stripped.startswith(marker):
            return True
    return False


async def is_fresh(client_id: int) -> bool:
    """У атлета ни одной законченной тренировки — смотреть модели не на что,
    опросник тогда фиксированный и без вызова модели (как у атлета с чистым
    листом, `ai_setup_flow.is_fresh_start`). Цель из его профиля тут не в
    счёт: тренер её не видит."""
    return await db.count_workouts(client_id) == 0
