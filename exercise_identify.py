"""«Что это за тренажёр»: упражнение по фото — одно ядро для бота и `/v1`.

Идея владельца: в новом зале снимаешь незнакомый тренажёр (или скриншот
упражнения из Instagram/YouTube) — тренер говорит, что это, даёт короткую
подсказку по настройке, и подход ложится в нужное упражнение; нет его в
каталоге — заводится своё.

Один платный вызов (`ai_trainer.vision_backend()`, строгая JSON-схема) через
`ai_trainer.paid_call` с квотой вопроса (`ai_limits.KIND_QUESTION`): фото
тренажёра — тот же вопрос тренеру, только с готовой формой ответа. Квота
списывается за состоявшийся ответ (`db.try_increment_ai_question_count`, вид
`identify`), как у `/ai/ask`.

Что видит модель. Каталог — русскими идентичностями шаблонов
(`seed_data.EXERCISE_TEMPLATES`, без скрытых) прямо в `enum` схемы: выбрать
можно только существующее, и идентичность приходит готовой, без перевода
обратно. Свои упражнения атлета не из каталога — списком в сообщении (они у
каждого свои и меняют только хвост запроса, шапка — промпт и схема — одна у
всех и ложится в кэш провайдера). Если ничего не подошло — модель предлагает
имя нового упражнения на языке атлета, группу (слаг встроенной группы) и
снаряд.

Чему модель не верим. Итог (`catalog`/`mine`/`new`/`none`) считает сервер по
базе атлета: есть у него копия этого шаблона — `mine` с её id, нет — `catalog`;
предложенное «новое» имя, которое уже есть у атлета или в каталоге, — тоже
`mine`/`catalog`, а не второе упражнение. Чисел в подсказке модель не пишет
(и предложения с цифрами сервер всё равно выбрасывает): единственная цифра —
«в прошлый раз 80×8» из настоящей истории атлета в этом упражнении.

«Записать сюда» (`accept`) ничего не вызывает у модели: копия шаблона —
`db.fork_exercise_from_template` (дедуп по идентичности на любом языке, архив
оживает), своё — `db.create_exercise` (дедуп по имени). Дубля не бывает.

События (`product_metrics.track`, без текста): `exercise_identified` и
`exercise_identify_accept` со свойством `result`. Клиент — из контекста входа
(`lifecycle_events.platform`); без него событие не пишется.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Awaitable, Callable, Optional

import ai_limits
import ai_trainer
import config
import db
import exercise_media
import formatting
import i18n
import lifecycle_events
import product_metrics
import rest_defaults
import seed_data

logger = logging.getLogger(__name__)

RESULT_CATALOG = "catalog"
RESULT_MINE = "mine"
RESULT_NEW = "new"
RESULT_NONE = "none"
RESULTS = (RESULT_CATALOG, RESULT_MINE, RESULT_NEW, RESULT_NONE)

EVENT_IDENTIFIED = "exercise_identified"
EVENT_ACCEPT = "exercise_identify_accept"

EQUIPMENT = rest_defaults.EQUIPMENT
MAX_ALTERNATIVES = 2
# Своих упражнений в запросе — не больше этого (самые ходовые первыми): дальше
# список только дорожает, а тренажёр на фото почти всегда из частых.
MAX_OWN_NAMES = 150
_MAX_TOKENS = 600
_MAX_TIP_SENTENCES = 2
_MAX_TIP_CHARS = 400
_MAX_TITLE_CHARS = 200
# Оценка входа на случай оборванного вызова (paid_call не меряет base64):
# картинка ~1100 токенов плюс промпт со схемой каталога.
_ABORT_PROMPT_TOKENS = 6000

SYSTEM_PROMPT = """You are the athlete's strength coach from a basement gym. The athlete sent one photo: a gym machine or piece of equipment, someone doing an exercise, or a screenshot from Instagram or YouTube. Work out which exercise it is.

Fill the JSON schema:
- unclear: true when the photo is not about training or you can't tell what exercise it is. Then catalog_name, own_name, new_name, new_group and equipment are null, alternatives is [], tip is "".
- catalog_name: the exercise from our catalog. Catalog names are fixed Russian identifiers: pick the one that matches the movement, the equipment and the position best. null when nothing in the catalog is this exercise.
- own_name: when one of the athlete's own exercises (listed in the message) is exactly this movement, copy its name character for character. Otherwise null.
- alternatives: up to two other catalog exercises it could also be (the same machine used another way, a close variant). [] when there are none.
- new_name and new_group: only when catalog_name and own_name are both null but the photo clearly shows an exercise or a machine — a short exercise name in the athlete's language and its muscle group. Otherwise null.
- equipment: what the exercise is done with — barbell, dumbbell, machine (lever, plate-loaded or selectorized), cable (cable stack, crossover), bodyweight, kettlebell, other. null only when unclear.
- title: one short sentence in the first person that names what it is, like "That's a hack squat machine." Not a question.
- tip: zero to two short sentences on how to set up and do it (seat, pads, grip, stance, range of motion). No numbers at all: no weights, reps, sets, seconds or degrees. Talk to the athlete informally, as "you"."""


class IdentifyFailed(Exception):
    """Модель ответила, но разобрать ответ нечем — квота не списывается."""


class AcceptError(Exception):
    """«Записать сюда» с негодным телом: машинный код и ключ текста тренера."""

    def __init__(self, code: str, key: str, status: int = 400, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.key = key
        self.status = status
        self.params = params


@dataclass
class Identification:
    result: str
    title: str
    tip: str
    exercise: Optional[dict[str, Any]] = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "title": self.title,
            "tip": self.tip,
            "exercise": self.exercise,
            "alternatives": self.alternatives,
        }


# ---------- каталог и схема ----------


@lru_cache(maxsize=None)
def catalog_identities() -> tuple[str, ...]:
    """Идентичности шаблонов, которые модель может назвать: весь каталог без
    скрытых (`seed_data.CATALOG_HIDDEN`). Без языка — застыть нечему."""
    return tuple(name for _group, name in seed_data.EXERCISE_TEMPLATES if not seed_data.is_catalog_hidden(name))


@lru_cache(maxsize=None)
def _identity_set() -> frozenset[str]:
    return frozenset(catalog_identities())


def group_slugs() -> tuple[str, ...]:
    return tuple(
        slug for slug in (seed_data.muscle_group_slug(name) for name, _emoji, _order in seed_data.MUSCLE_GROUP_PRESETS)
        if slug
    )


@lru_cache(maxsize=None)
def _canonical_group_by_slug() -> dict[str, str]:
    return {
        seed_data.muscle_group_slug(name): name
        for name, _emoji, _order in seed_data.MUSCLE_GROUP_PRESETS
        if seed_data.muscle_group_slug(name)
    }


@lru_cache(maxsize=None)
def response_format() -> dict[str, Any]:
    """Строгая схема ответа (structured outputs: все поля required,
    additionalProperties false). Одинаковая у всех — часть кэшируемой шапки."""
    identities = list(catalog_identities())
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "exercise_identification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "unclear": {"type": "boolean"},
                    "catalog_name": {"type": ["string", "null"], "enum": [*identities, None]},
                    "own_name": {"type": ["string", "null"]},
                    "alternatives": {"type": "array", "items": {"type": "string", "enum": identities}},
                    "new_name": {"type": ["string", "null"]},
                    "new_group": {"type": ["string", "null"], "enum": [*group_slugs(), None]},
                    "equipment": {"type": ["string", "null"], "enum": [*EQUIPMENT, None]},
                    "title": {"type": "string"},
                    "tip": {"type": "string"},
                },
                "required": [
                    "unclear", "catalog_name", "own_name", "alternatives", "new_name", "new_group",
                    "equipment", "title", "tip",
                ],
                "additionalProperties": False,
            },
        },
    }


# ---------- разбор ответа ----------


def _fold(name: Optional[str]) -> str:
    return db._fold_exercise_name((name or "").strip())


_SENTENCE = re.compile(r"[^.!?…]+[.!?…]*")
_DIGIT = re.compile(r"\d")


def clean_tip(tip: Any) -> str:
    """Подсказка без чисел (вес, повторы, секунды мы атлету не выдумываем) и не
    длиннее двух предложений."""
    if not isinstance(tip, str):
        return ""
    sentences = [s.strip() for s in _SENTENCE.findall(formatting.strip_control_chars(tip))]
    kept = [s for s in sentences if s and not _DIGIT.search(s)][:_MAX_TIP_SENTENCES]
    return " ".join(kept)[:_MAX_TIP_CHARS].strip()


def _clean_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    return formatting.strip_control_chars(title).strip()[:_MAX_TITLE_CHARS]


def _lang_of(user: Any) -> str:
    return user["lang"] if user is not None and user["lang"] else i18n.DEFAULT_LANG


async def _own_names(user_id: int) -> list[str]:
    """Свои упражнения атлета не из каталога (и копии скрытых шаблонов) —
    их модель может назвать в `own_name`."""
    rows = await db.list_user_exercises(user_id)
    names = []
    for row in rows:
        if row["original_name"] in _identity_set():
            continue
        names.append(row["display_name"])
        if len(names) >= MAX_OWN_NAMES:
            break
    return names


def _user_message(own_names: list[str], image_data_url: str) -> list[dict[str, Any]]:
    if own_names:
        text = "The athlete's own exercises outside the catalog: " + "; ".join(own_names) + "."
    else:
        text = "The athlete has no own exercises outside the catalog."
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]


async def _model_verdict(
    user_id: int,
    image_data_url: str,
    own_names: list[str],
    *,
    on_block: Optional[Callable[[ai_limits.Block], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """Один платный вызов: фото → разобранный JSON по схеме. LimitBlocked —
    настоящий отказ ai_limits до вызова; IdentifyFailed — ответ не разобрать."""
    backend = ai_trainer.vision_backend()
    messages = [
        # Язык ответа — хвостом системного промпта (_with_language_tail): шапка
        # до него одинакова у всех языков.
        {"role": "system", "content": ai_trainer._with_language_tail(SYSTEM_PROMPT)},
        {"role": "user", "content": _user_message(own_names, image_data_url)},
    ]
    kwargs = ai_trainer._model_kwargs(backend, user_id, max_tokens=_MAX_TOKENS, quick=True, scope="identify")
    response = await ai_trainer.paid_call(
        user_id, ai_limits.KIND_QUESTION,
        lambda: backend.client.chat.completions.create(
            **kwargs, messages=messages, response_format=response_format(),
        ),
        model=backend.model,
        source="ios" if lifecycle_events.platform() == lifecycle_events.PLATFORM_IOS else "bot",
        on_block=on_block,
        abort_prompt_tokens=_ABORT_PROMPT_TOKENS,
    )
    try:
        data = ai_trainer._extract_json_object(response.choices[0].message.content or "")
    except (ValueError, IndexError, AttributeError) as exc:
        raise IdentifyFailed("unparsable identification") from exc
    if not isinstance(data, dict):
        raise IdentifyFailed("identification is not an object")
    return data


# ---------- сборка ответа по базе атлета ----------


def _image_url_for_identity(identity: Optional[str]) -> Optional[str]:
    if not identity:
        return None
    images = exercise_media.get_images(identity)
    return exercise_media.media_url(images[0]) if images else None


async def _group_name_of_row(row: Any, lang: str) -> str:
    group = await db.get_muscle_group(row["primary_group_id"]) if row["primary_group_id"] else None
    if group is None:
        return seed_data.localized_muscle_group_name(seed_data.OTHER_GROUP_NAME, lang)
    return seed_data.localized_muscle_group_name(group["name"], lang)


async def _live_fork(user_id: int, identity: str) -> Optional[Any]:
    """Своя живая (не в архиве) копия шаблона — на любом языке."""
    row = await db.find_exercise_by_original_name(user_id, identity)
    return row if row is not None and not row["is_archived"] else None


async def _catalog_exercise(user_id: int, identity: str, lang: str) -> tuple[str, dict[str, Any], Any]:
    """(итог, объект exercise, своя строка или None) для шаблона каталога."""
    own = await _live_fork(user_id, identity)
    group = rest_defaults.catalog_group(identity) or seed_data.OTHER_GROUP_NAME
    exercise = {
        "exercise_id": own["id"] if own is not None else None,
        "name": own["display_name"] if own is not None else seed_data.localized_exercise_name(identity, lang),
        "catalog_name": identity,
        "muscle_group": (
            await _group_name_of_row(own, lang) if own is not None
            else seed_data.localized_muscle_group_name(group, lang)
        ),
        "equipment": rest_defaults.equipment_of(identity) or "other",
        "image_url": _image_url_for_identity(identity),
    }
    return (RESULT_MINE if own is not None else RESULT_CATALOG), exercise, own


async def _own_exercise(row: Any, lang: str, equipment: Optional[str]) -> dict[str, Any]:
    identity = rest_defaults.exercise_identity(row)
    images = exercise_media.get_images_for(row)
    return {
        "exercise_id": row["id"],
        "name": row["display_name"],
        "catalog_name": identity,
        "muscle_group": await _group_name_of_row(row, lang),
        "equipment": (rest_defaults.equipment_of(identity) if identity else None) or equipment or "other",
        "image_url": exercise_media.media_url(images[0]) if images else None,
    }


async def _catalog_by_any_name(name: str) -> Optional[str]:
    """Идентичность шаблона по имени на любом языке (показанное → русское)."""
    found = await db.find_global_templates_by_names([name])
    template = found.get(name)
    if template is None or template["name"] not in _identity_set():
        return None
    return template["name"]


async def _last_time_line(user_id: int, exercise_id: int) -> Optional[str]:
    """«В прошлый раз 80×8» — лучший подход последней законченной тренировки
    с этим упражнением. Нет истории — нет строки: цифру мы не выдумываем."""
    workout_id = await db.find_last_finished_workout_with_exercise(user_id, exercise_id)
    if workout_id is None:
        return None
    sets = await db.list_sets_for_workout_exercise(workout_id, exercise_id)
    if not sets:
        return None
    best = max(sets, key=lambda s: ((s["weight"] or 0), (s["reps"] or 0)))
    if not best["reps"]:
        return None
    return i18n.t("exercise_identify.last_time", set=formatting.format_set(best["weight"] or 0, best["reps"]))


async def _alternatives(user_id: int, names: Any, main_identity: Optional[str], lang: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = {main_identity} if main_identity else set()
    for identity in names if isinstance(names, list) else []:
        if identity not in _identity_set() or identity in seen:
            continue
        seen.add(identity)
        own = await _live_fork(user_id, identity)
        out.append({
            "exercise_id": own["id"] if own is not None else None,
            "name": own["display_name"] if own is not None else seed_data.localized_exercise_name(identity, lang),
            "catalog_name": identity,
        })
        if len(out) >= MAX_ALTERNATIVES:
            break
    return out


async def resolve(user_id: int, verdict: dict[str, Any], lang: str) -> Identification:
    """Ответ модели → итог по базе атлета (без платных вызовов и без записи)."""
    equipment = verdict.get("equipment") if verdict.get("equipment") in EQUIPMENT else None
    title = _clean_title(verdict.get("title"))
    tip = clean_tip(verdict.get("tip"))

    result, exercise, own_row = RESULT_NONE, None, None
    identity = verdict.get("catalog_name")
    if not verdict.get("unclear"):
        if isinstance(identity, str) and identity in _identity_set():
            result, exercise, own_row = await _catalog_exercise(user_id, identity, lang)
        else:
            identity = None
            own_name = verdict.get("own_name")
            row = await db.find_exercise_by_name(user_id, own_name) if isinstance(own_name, str) and own_name.strip() else None
            if row is not None:
                result, own_row = RESULT_MINE, row
                exercise = await _own_exercise(row, lang, equipment)
                identity = exercise["catalog_name"]
            else:
                new_name = formatting.strip_control_chars(verdict.get("new_name") or "").strip()
                if new_name and len(new_name) <= config.MAX_EXERCISE_NAME_LENGTH:
                    row = await db.find_exercise_by_name(user_id, new_name)
                    catalog = None if row is not None else await _catalog_by_any_name(new_name)
                    if row is not None:
                        result, own_row = RESULT_MINE, row
                        exercise = await _own_exercise(row, lang, equipment)
                        identity = exercise["catalog_name"]
                    elif catalog is not None:
                        identity = catalog
                        result, exercise, own_row = await _catalog_exercise(user_id, catalog, lang)
                    else:
                        group = _canonical_group_by_slug().get(verdict.get("new_group") or "", seed_data.OTHER_GROUP_NAME)
                        result = RESULT_NEW
                        exercise = {
                            "exercise_id": None,
                            "name": new_name,
                            "catalog_name": None,
                            "muscle_group": seed_data.localized_muscle_group_name(group, lang),
                            "equipment": equipment or "other",
                            "image_url": None,
                        }

    if result == RESULT_NONE:
        # Что случилось и что сделать — своим текстом, а не догадкой модели.
        return Identification(RESULT_NONE, i18n.t("exercise_identify.none_title"), "", None, [])

    if not title:
        title = i18n.t("exercise_identify.title_fallback", name=exercise["name"])
    if own_row is not None:
        last_time = await _last_time_line(user_id, own_row["id"])
        if last_time:
            tip = f"{tip} {last_time}".strip()
    alternatives = await _alternatives(user_id, verdict.get("alternatives"), identity, lang)
    return Identification(result, title, tip, exercise, alternatives)


async def _track(user_id: int, event: str, result: str) -> None:
    platform = lifecycle_events.platform()
    if platform is None:
        return
    await product_metrics.track(
        user_id, event, {"result": result}, platform=platform, app_version=lifecycle_events.app_version(),
    )


async def identify(
    user_id: int,
    image_data_url: str,
    *,
    on_block: Optional[Callable[[ai_limits.Block], Awaitable[None]]] = None,
) -> Identification:
    """Фото → что это за упражнение у этого атлета. Одна точка у бота и `/v1`.

    Платный вызов с проверкой квоты вопроса и HARD-стопа (paid_call поднимет
    `ai_trainer.LimitBlocked` до вызова); после ответа — списание вопроса и
    событие `exercise_identified`. Текст ответа — на языке атлета (users.lang)."""
    user = await db.get_user(user_id)
    lang = _lang_of(user)
    with i18n.use_lang(lang):
        own_names = await _own_names(user_id)
        verdict = await _model_verdict(user_id, image_data_url, own_names, on_block=on_block)
        identification = await resolve(user_id, verdict, lang)
    # Вопрос тренеру состоялся — списываем, как /ai/ask (атомарно, не выше лимита).
    await db.try_increment_ai_question_count(user_id, config.AI_QUESTION_DAILY_LIMIT, kind="identify")
    await _track(user_id, EVENT_IDENTIFIED, identification.result)
    return identification


# ---------- «Записать сюда» ----------


async def resolve_group_id(user_id: int, value: Any) -> Optional[int]:
    """Группа мышц из тела: слаг встроенной (`chest`), её имя на любом языке
    («Грудь», «Chest») или имя своей группы атлета. None — не нашёл."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    canonical = (
        _canonical_group_by_slug().get(text.lower())
        or seed_data.canonical_muscle_group_name(text)
        or next((name for name, _e, _o in seed_data.MUSCLE_GROUP_PRESETS if _fold(name) == _fold(text)), None)
    )
    if canonical is not None:
        return await db.builtin_muscle_group_id(canonical)
    for group in await db.list_muscle_groups(user_id):
        if group["user_id"] == user_id and _fold(group["name"]) == _fold(text):
            return group["id"]
    return None


def _exercise_name(value: Any) -> str:
    name = formatting.strip_control_chars(value if isinstance(value, str) else "").strip()
    if not name:
        raise AcceptError("bad_request", "api.error.name_empty")
    if len(name) > config.MAX_EXERCISE_NAME_LENGTH:
        raise AcceptError("name_too_long", "api.error.name_too_long", max=config.MAX_EXERCISE_NAME_LENGTH)
    return name


async def _accept_catalog(user_id: int, identity: str) -> tuple[int, str]:
    found = await db.find_global_templates_by_names([identity])
    template = found.get(identity)
    if template is None:
        raise AcceptError("unknown_catalog_exercise", "exercise_identify.unknown_catalog")
    existed = await _live_fork(user_id, template["name"]) is not None
    exercise_id = await db.fork_exercise_from_template(user_id, template["id"])
    return exercise_id, RESULT_MINE if existed else RESULT_CATALOG


async def accept(
    user_id: int,
    *,
    catalog_name: Any = None,
    name: Any = None,
    muscle_group: Any = None,
    equipment: Any = None,
) -> tuple[Any, str]:
    """Завести (или найти уже заведённое) упражнение по итогу распознавания.

    `catalog_name` — копия шаблона на языке атлета; иначе `name` +
    `muscle_group` + `equipment` — своё упражнение. Имя, которое уже есть у
    атлета или в каталоге, ведёт в то, что есть: дубля не бывает. Возвращает
    (строка exercises, итог mine|catalog|new). Без платных вызовов."""
    if catalog_name is not None:
        if not isinstance(catalog_name, str) or not catalog_name.strip():
            raise AcceptError("unknown_catalog_exercise", "exercise_identify.unknown_catalog")
        exercise_id, result = await _accept_catalog(user_id, catalog_name.strip())
    else:
        exercise_name = _exercise_name(name)
        if equipment not in EQUIPMENT:
            raise AcceptError("invalid_equipment", "exercise_identify.invalid_equipment")
        group_id = await resolve_group_id(user_id, muscle_group)
        if group_id is None:
            raise AcceptError("invalid_muscle_group", "exercise_identify.invalid_group")
        own = await db.find_exercise_by_name(user_id, exercise_name)
        identity = None if own is not None else await _catalog_by_any_name(exercise_name)
        if own is not None:
            exercise_id, result = own["id"], RESULT_MINE
        elif identity is not None:
            exercise_id, result = await _accept_catalog(user_id, identity)
        else:
            existed = await db.find_exercise_by_display_name(user_id, exercise_name) is not None
            exercise_id = await db.create_exercise(user_id, exercise_name, group_id)
            result = RESULT_MINE if existed else RESULT_NEW
            if not existed and equipment == "bodyweight":
                # Своим весом — та же галка, что «Своим весом» при создании.
                await db.set_exercise_bodyweight(exercise_id, True)
    row = await db.get_exercise(exercise_id)
    await _track(user_id, EVENT_ACCEPT, result)
    return row, result
