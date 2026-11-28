"""«Похоже, это дубль»: на какое упражнение каталога похоже своё упражнение.

Человек завёл «conventional deadlift» руками (или принёс из Hevy), а в каталоге
есть «Становая тяга» — с картинкой, техникой и идентичностью, по которой
считаются замены, отдых и мышцы. Приложение показывает кандидатов наверху листа
«Объединить» и подсказкой на карточке своего упражнения; выбор — форк шаблона
(`POST /exercise-templates/{id}/add`) и обычное объединение в него
(`POST /exercises/merge`), здесь ничего не пишется, кроме кэша ответа.

Гибрид, чтобы платить только за трудные имена:

1. Резолвер импорта (`handlers.csv_import`: точное имя шаблона на любом языке,
   словарь Hevy, те же слова в другом порядке и форме) — бесплатно. Уверенный
   ответ («sure») отдаётся сразу, модель не зовётся.
2. Иначе — ОДИН короткий вызов Luna (`ai_trainer.paid_call`, вид
   `ai_limits.KIND_MERGE_SUGGEST`, chat/completions с effort `none`, строгая
   схема). Модель видит имя, группу и снаряд упражнения и короткий список
   каталога (`SHORTLIST_SIZE`: похожие по резолверу, потом по общим словам,
   потом той же группы); ответ — id только из этого списка (`enum` схемы).
   Ответ кэшируется в `exercises.dup_suggest` под именем, для которого
   спрашивали: повторное открытие экрана не платит, переименование —
   спрашивает заново.
3. Квота кончилась, деньги на сегодня кончились, модель не настроена или
   ответила мусором — молча «maybe» резолвера. Ошибки атлет не видит никогда.

Шаблон, копия которого у атлета уже живая, предлагается под именем этой копии
(«Barbell Deadlift», а не «Становая тяга»): объединять своё «conventional
deadlift» надо именно в неё, и `POST /exercise-templates/{id}/add` на уже
форкнутый шаблон возвращает ту же копию. Раньше такие шаблоны выкидывались из
списка, и модель, не видя становую, честно отвечала «ничего» (владелец:
«странно, что мерж не распознал такое простое»). Не предлагаются скрытые
шаблоны каталога. У упражнения, которое само копия каталога (`original_name` —
идентичность), кандидатов нет.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import ai_limits
import ai_trainer
import db
import exercise_media
import lifecycle_events
import rest_defaults
import search_terms
import seed_data
from handlers import csv_import

logger = logging.getLogger(__name__)

SURE = "sure"
MAYBE = "maybe"
CONFIDENCES = (SURE, MAYBE)

MAX_CANDIDATES = 3
# Сколько шаблонов видит модель: хватает, чтобы правильный почти всегда был
# в списке, и запрос остаётся коротким.
SHORTLIST_SIZE = 30
_MAX_TOKENS = 300
# 2 — шаблоны со своей копией снова в списке: старые пустые ответы спросить заново.
_CACHE_VERSION = 2

SYSTEM_PROMPT = """You match a strength athlete's self-made exercise to the gym's exercise catalog.

The message has the athlete's exercise (name as typed, muscle group, equipment when known) and a shortlist of catalog exercises, each with an id and a name. Names may be in any language, abbreviated, or carry gym shorthand: "1arm" is one arm, "cuff" is an ankle or wrist strap on a cable, "block" or "pulley" is a cable machine, "flat" is a flat bench, "conventional" is the standard deadlift stance.

Return up to three catalog ids that are the SAME exercise: the same movement, the same equipment (barbell, dumbbell, cable, machine, bodyweight) and the same position where the catalog tells them apart. Best match first.
- confidence "sure": clearly the same exercise, merging the two histories is safe.
- confidence "maybe": the same movement but a detail is unclear (the name doesn't say the bench angle, grip or equipment).
Leave out anything that is a different movement or different equipment. An empty list is a good answer when nothing fits."""


@dataclass(frozen=True)
class Pick:
    identity: str
    confidence: str


def _fold(value: Optional[str]) -> str:
    return search_terms.fold(value or "")


def _is_catalog_copy(row: Any) -> bool:
    return rest_defaults.is_catalog_identity(row["original_name"])


async def _catalog_for(user_id: int) -> list[Any]:
    """Шаблоны, которые можно предложить: весь каталог без скрытых, в том
    числе те, чья копия у атлета уже есть (их покажем под именем копии)."""
    return [
        t for t in await db.list_all_exercise_templates()
        if t["user_id"] is None
        and not seed_data.is_catalog_hidden(t["name"])
    ]


def _by_popularity(rows: list[Any]) -> list[Any]:
    return sorted(rows, key=lambda t: seed_data.catalog_popularity(t["name"]))


async def resolver_picks(name: str, catalog: list[Any]) -> tuple[Optional[Pick], list[Pick], list[Any]]:
    """Бесплатный шаг: (уверенный или None, «maybe» лучшие первыми, похожие
    шаблоны для короткого списка модели). Уверенный может оказаться вне
    catalog (скрытый шаблон) — тогда вызывающий не предлагает ничего.

    Уверенно — то же, что резолвер импорта считает точным: имя шаблона на
    любом языке, стандартное имя Hevy, те же слова в другом порядке и форме
    ровно у одного шаблона. «Вперёд»/«назад» по словам (в названии есть все
    слова имени или наоборот) — только «maybe»: лишнее или пропущенное слово
    часто и есть другое движение (гантели против штанги, Смит)."""
    allowed = {t["name"]: t for t in catalog}
    exact = (await db.find_global_templates_by_names([name])).get(name)
    identity = exact["name"] if exact is not None else seed_data.hevy_catalog_identity(name)
    groups = search_terms.query_groups(name)
    similar: list[Any] = []
    if groups:
        chosen, similar = csv_import._pick(name, groups, catalog, csv_import._template_texts)
        if identity is None and chosen is not None:
            identity = chosen["name"]
    # Внутри каждой ступени резолвера — ходовые первыми: «bench press» —
    # это скорее жим штанги лёжа, чем жим в Смите на наклонной.
    same = [t for t in similar if any(csv_import._same_words(name, x) for x in csv_import._template_texts(t))]
    rest = [t for t in similar if t not in same]
    similar = _by_popularity(same) + _by_popularity(rest)
    sure = Pick(identity, SURE) if identity is not None and rest_defaults.is_catalog_identity(identity) else None
    maybes = [
        Pick(t["name"], MAYBE) for t in similar
        if t["name"] in allowed and (sure is None or t["name"] != sure.identity)
    ]
    return sure, maybes[:MAX_CANDIDATES], similar


def shortlist(name: str, group_id: Optional[int], catalog: list[Any], similar: list[Any]) -> list[Any]:
    """Короткий список для модели: похожие по резолверу → шаблоны с общими
    словами (больше общих — выше) → той же группы мышц, ходовые первыми."""
    out: list[Any] = []
    seen: set[int] = set()

    def add(rows: list[Any]) -> None:
        for row in rows:
            if len(out) >= SHORTLIST_SIZE:
                return
            if row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)

    add(similar)
    groups = search_terms.query_groups(name)
    if groups:
        scored = []
        for row in catalog:
            folded = [_fold(t) for t in csv_import._template_texts(row)]
            hits = sum(1 for variants in groups if any(v in f for f in folded for v in variants))
            if hits:
                scored.append((-hits, seed_data.catalog_popularity(row["name"]), row))
        scored.sort(key=lambda item: item[:2])
        add([row for *_k, row in scored])
    if group_id is not None:
        add(_by_popularity([t for t in catalog if t["primary_group_id"] == group_id]))
    return out


def response_format(ids: list[str]) -> dict[str, Any]:
    """Строгая схема: id — только из присланного списка."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "catalog_duplicates",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "enum": ids},
                                "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                            },
                            "required": ["id", "confidence"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["matches"],
                "additionalProperties": False,
            },
        },
    }


async def _group_name(group_id: Optional[int], lang: str) -> Optional[str]:
    group = await db.get_muscle_group(group_id) if group_id else None
    if group is None:
        return None
    return seed_data.localized_muscle_group_name(group["name"], lang)


async def model_picks(user_id: int, row: Any, lang: str, short: list[Any]) -> Optional[list[Pick]]:
    """Один платный вызов. None — не спросили или ответ не разобрать (тогда
    вызывающий отдаёт «maybe» резолвера и ничего не кэширует)."""
    if not short or not ai_trainer.is_configured():
        return None
    by_id = {str(t["id"]): t for t in short}
    exercise: dict[str, Any] = {"name": row["display_name"]}
    group = await _group_name(row["primary_group_id"], lang)
    if group:
        exercise["muscle_group"] = group
    if row["equipment"]:
        exercise["equipment"] = row["equipment"]
    catalog = [
        {"id": key, "name": seed_data.localized_exercise_name(t["name"], lang)}
        for key, t in by_id.items()
    ]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"exercise": exercise, "catalog": catalog}, ensure_ascii=False)},
    ]
    try:
        backend = ai_trainer.luna_backend()
        kwargs = ai_trainer._model_kwargs(backend, user_id, max_tokens=_MAX_TOKENS, quick=True, scope="dup_suggest")
        response = await ai_trainer.paid_call(
            user_id, ai_limits.KIND_MERGE_SUGGEST,
            lambda: backend.client.chat.completions.create(
                **kwargs, messages=messages, response_format=response_format(list(by_id)),
            ),
            model=backend.model,
            source="ios" if lifecycle_events.platform() == lifecycle_events.PLATFORM_IOS else "bot",
            messages=messages,
        )
    except ai_trainer.LimitBlocked as exc:
        logger.info("dup suggest skipped for user %s: %s", user_id, exc.block.log)
        return None
    except Exception:
        logger.exception("dup suggest call failed for user %s", user_id)
        return None
    await db.increment_ai_usage(user_id, ai_limits.KIND_MERGE_SUGGEST)
    try:
        data = ai_trainer._extract_json_object(response.choices[0].message.content or "")
    except (ValueError, IndexError, AttributeError):
        return None
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return None
    picks: list[Pick] = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        template = by_id.get(str(item.get("id")))
        confidence = item.get("confidence")
        # Схема строгая, но ответ — вторая линия обороны: id не из списка
        # ничем не лучше выдумки.
        if template is None or confidence not in CONFIDENCES:
            continue
        if any(p.identity == template["name"] for p in picks):
            continue
        picks.append(Pick(template["name"], confidence))
    return picks[:MAX_CANDIDATES]


def _cached(row: Any) -> Optional[list[Pick]]:
    raw = row["dup_suggest"]
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != _CACHE_VERSION or data.get("name") != row["display_name"]:
        return None
    picks = []
    for item in data.get("picks") or []:
        if isinstance(item, dict) and item.get("confidence") in CONFIDENCES and isinstance(item.get("identity"), str):
            picks.append(Pick(item["identity"], item["confidence"]))
    return picks


async def _store(row: Any, picks: list[Pick]) -> None:
    payload = {
        "v": _CACHE_VERSION,
        "name": row["display_name"],
        "picks": [{"identity": p.identity, "confidence": p.confidence} for p in picks],
    }
    await db.set_exercise_dup_suggest(row["id"], json.dumps(payload, ensure_ascii=False))


async def picks_for(user_id: int, row: Any, lang: str, *, use_model: bool = True) -> list[Pick]:
    """Кандидаты для своего упражнения row (без проверки владения).
    use_model=False — согласия отдавать данные модели нет: только резолвер,
    закэшированный ответ модели (он уже получен с согласием) остаётся в силе."""
    if row["is_template"] or _is_catalog_copy(row):
        return []
    catalog = await _catalog_for(user_id)
    allowed = {t["name"] for t in catalog}
    sure, maybes, similar = await resolver_picks(row["display_name"], catalog)
    if sure is not None:
        if sure.identity not in allowed:
            return []
        return [sure, *maybes][:MAX_CANDIDATES]
    cached = _cached(row)
    if cached is None:
        if not use_model:
            return maybes
        short = shortlist(row["display_name"], row["primary_group_id"], catalog, similar)
        answered = await model_picks(user_id, row, lang, short)
        if answered is None:
            return maybes
        await _store(row, answered)
        cached = answered
    # Шаблон могли скрыть из каталога после ответа модели — его не предлагаем.
    return [p for p in cached if p.identity in allowed][:MAX_CANDIDATES]


async def candidates_json(
    user_id: int, row: Any, lang: str, *, use_model: bool = True,
) -> list[dict[str, Any]]:
    """Ответ `GET /exercises/{id}/catalog-duplicates`: имена и группы — на
    языке атлета, `original_name` — идентичность (не для показа)."""
    picks = await picks_for(user_id, row, lang, use_model=use_model)
    if not picks:
        return []
    templates = await db.find_global_templates_by_names([p.identity for p in picks])
    # Своя живая копия шаблона — показываем её имя: объединять будем в неё.
    own_names = {
        _fold(r["original_name"]): r["display_name"]
        for r in await db.list_active_exercises_with_identity(user_id)
        if r["id"] != row["id"]
    }
    out = []
    for pick in picks:
        template = templates.get(pick.identity)
        if template is None:
            continue
        group = rest_defaults.catalog_group(pick.identity) or seed_data.OTHER_GROUP_NAME
        out.append({
            "template_id": template["id"],
            "name": own_names.get(_fold(template["name"])) or seed_data.localized_exercise_name(template["name"], lang),
            "original_name": template["name"],
            "group": seed_data.localized_muscle_group_name(group, lang),
            "group_id": template["primary_group_id"],
            "thumb": exercise_media.thumb_url_for(template),
            "confidence": pick.confidence,
        })
    return out
