"""Сохранение черновика программы, собранного тренером (ai_trainer.propose_program).

Логика записи в БД одна на бота (handlers/ai_trainer.py — кнопки «Забрать»/
«Добавить себе») и на REST `/v1` (api_v1_ai.py — POST /ai/program/save):
черновик — просто dict {"name", "description", "days", "replaces", ...},
никакого aiogram или Telegram в этом модуле нет и быть не должно (см. приём
progression_data.py — общая логика отдельно от того, кто её вызывает).

Экранная часть — что показать пользователю после сохранения, какую кнопку
дать при конфликте имени — остаётся в каждом интерфейсе своя; здесь только
факт записи и её результат.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, Optional

import db


async def create_program_day(user_id: int, day: dict[str, Any], program_id: int) -> int:
    """Создать один день программы и, если тренер задал прогрессию хоть на одно
    упражнение, записать её (db.set_routine_exercise_progression).

    Порядок routine_exercises после create_routine_from_program совпадает с
    порядком day["items"] (тот же список, без пропусков — дубли и нерезолвнутые
    имена уже отфильтрованы в ai_trainer._propose_program), поэтому сверяем по
    display_name, а не по позиции — устойчивее, если это когда-нибудь перестанет
    быть так.
    """
    routine_id = await db.create_routine_from_program(
        user_id, day["name"],
        [(item["name"], item.get("target")) for item in day["items"]],
        program_id=program_id,
    )
    progressions = {
        item["name"]: item["progression"] for item in day["items"] if item.get("progression")
    }
    if progressions:
        for re_row in await db.list_routine_exercises(routine_id):
            progression = progressions.get(re_row["display_name"])
            if progression:
                await db.set_routine_exercise_progression(
                    re_row["id"], json.dumps(progression, ensure_ascii=False)
                )
    return routine_id


def match_days(
    new_days: list[dict[str, Any]],
    old_days: list[Any],
    new_ex_ids: Optional[list[set[int]]] = None,
    old_ex_ids: Optional[dict[int, set[int]]] = None,
) -> list[Optional[int]]:
    """Для каждого нового дня — id старого дня, который он обновляет, или None.

    Сначала по имени без регистра и пробелов. Из несовпавших по имени пара
    подставляется по порядку, только если (а) несовпавших ровно по одному с
    каждой стороны (это переименование), либо (б) состав упражнений совпадает
    хотя бы наполовину (по exercise_id). Иначе — новый день: слепая подстановка
    по порядку склеила бы историю чужого дня с переименованным и переставленным.
    """
    def key(name: str) -> str:
        return name.strip().lower()

    by_name = {key(old["name"]): old["id"] for old in reversed(old_days)}
    taken: set[int] = set()
    result: list[Optional[int]] = []
    for day in new_days:
        old_id = by_name.get(key(day["name"]))
        if old_id is not None and old_id not in taken:
            taken.add(old_id)
            result.append(old_id)
        else:
            result.append(None)

    free_old = [old["id"] for old in old_days if old["id"] not in taken]
    free_new = [i for i, r in enumerate(result) if r is None]
    if len(free_old) == 1 and len(free_new) == 1:
        result[free_new[0]] = free_old[0]
    elif new_ex_ids is not None and old_ex_ids is not None:
        for i in free_new:
            mine = new_ex_ids[i]
            best, best_share = None, 0.5
            for old_id in free_old:
                theirs = old_ex_ids.get(old_id, set())
                union = mine | theirs
                share = len(mine & theirs) / len(union) if union else 0.0
                if share >= best_share and old_id not in taken:
                    best, best_share = old_id, share
            if best is not None:
                taken.add(best)
                result[i] = best
    return result


async def _plan_days_in_place(
    user_id: int, days: list[dict[str, Any]], old_days: list[Any]
) -> list[dict[str, Any]]:
    """Планы дней для db.replace_program_days: упражнения заранее
    резолвятся в id (дубли и неразрешимые имена отбрасываются, как в
    db.create_routine_from_program), чтобы сама запись была одной транзакцией."""
    resolved: list[list[tuple[int, Optional[str], Optional[str]]]] = []
    for day in days:
        seen: set[int] = set()
        exercises = []
        for item in day["items"]:
            ex_id = await db.get_or_create_user_exercise_by_name(user_id, item["name"])
            if ex_id is None or ex_id in seen:
                continue
            seen.add(ex_id)
            progression = item.get("progression")
            exercises.append((
                ex_id, item.get("target"),
                json.dumps(progression, ensure_ascii=False) if progression else None,
            ))
        resolved.append(exercises)
    old_ex_ids = {
        old["id"]: {row["exercise_id"] for row in await db.list_routine_exercises(old["id"])}
        for old in old_days
    }
    matches = match_days(days, old_days, [{e[0] for e in ex} for ex in resolved], old_ex_ids)
    return [
        {"routine_id": old_id, "name": day["name"], "exercises": exercises}
        for day, old_id, exercises in zip(days, matches, resolved, strict=True)
    ]


async def save_into_existing_program(
    user_id: int, draft: dict[str, Any], program: Any
) -> dict[str, Any]:
    """Правка уже сохранённой программы — резолвится по актуальному состоянию
    программы, а не по снимку, сделанному при предложении (см. A7 в
    handlers/ai_trainer.py._save_into_existing_program): день, который
    пользователь успел добавить руками между предложением и тапом/запросом,
    не должен переживать замену — заменяется весь текущий набор дней.

    Возвращает `{"program_id", "day_count", "name", "replacing": True}` либо
    `{"error": "budget", "message": ...}`, если лимит программ не пускает.
    """
    days = draft["days"]
    old_days = await db.list_program_days_by_id(program["id"])
    budget_msg = await db.routine_budget(user_id, adding=len(days), freeing=len(old_days))
    if budget_msg:
        return {"error": "budget", "message": budget_msg}

    # Переименование — только если тренер прислал ДРУГОЕ имя, чем то, что видела
    # модель при предложении (draft["replaces"]["name"]), а не отличное от
    # текущего живого имени: сравнение с live-именем спутало бы «модель хочет
    # переименовать» с «пользователь успел переименовать руками сам».
    resolved_name = (draft.get("replaces") or {}).get("name") or program["name"]
    renamed_by_trainer = draft["name"].strip().lower() != resolved_name.strip().lower()

    # Дни обновляются НА МЕСТЕ: id дня — то, на что ссылаются тренировки
    # (workouts.routine_id), «следующий день» и адхеренс. Сопоставление — по
    # имени дня (как formatting.build_program_changes), потом осторожно по
    # порядку (см. match_days). Имя, описание, разгрузка и дни пишутся одной
    # транзакцией (db.replace_program_days): упало — программа целиком прежняя.
    # Разгрузка — часть программы целиком: не прислал — её больше нет.
    plans = await _plan_days_in_place(user_id, days, old_days)
    matched = {plan["routine_id"] for plan in plans if plan["routine_id"] is not None}
    _, renamed = await db.replace_program_days(
        user_id, program["id"], plans,
        [old["id"] for old in old_days if old["id"] not in matched],
        rename_to=draft["name"] if renamed_by_trainer else None,
        description=draft.get("description"),
        deload_every_weeks=draft.get("deload_every_weeks"),
    )
    target_name = draft["name"] if renamed else program["name"]

    final_days = await db.list_program_days_by_id(program["id"])
    return {
        "program_id": program["id"], "day_count": len(final_days),
        "name": target_name, "replacing": True,
    }


async def save_as_new_program(
    user_id: int, draft: dict[str, Any], freeing_routine_id: Optional[int] = None
) -> dict[str, Any]:
    """Новая программа — включая замену одиночной (однодневной) программы:
    у неё нет program_id, поэтому заводится новая, а старый день удаляется
    отдельно (`freeing_routine_id`).

    Возвращает `{"program_id", "day_count", "name", "replacing": False}`,
    `{"error": "budget", "message": ...}` при нехватке лимита программ, или
    `{"error": "name_conflict", "name": ...}`, если имя уже занято другой
    программой пользователя — решает тогда сам вызывающий (заменить/копия).
    """
    days = draft["days"]
    freed = 1 if freeing_routine_id else 0
    budget_msg = await db.routine_budget(user_id, adding=len(days), freeing=freed)
    if budget_msg:
        return {"error": "budget", "message": budget_msg}

    program_id = await db.create_program(
        user_id, draft["name"], source="ai", description=draft.get("description"),
        deload_every_weeks=draft.get("deload_every_weeks"),
    )
    if program_id is None:
        return {"error": "name_conflict", "name": draft["name"]}

    # Дни пишутся отдельными запросами, не одной транзакцией с create_program —
    # свежесозданную программу без части дней снаружи не отличить от целой,
    # поэтому при падении посередине убираем обрубок целиком сами, а не
    # заставляем каждого вызывающего (бота и REST) помнить об этом отдельно.
    try:
        for day in days:
            await create_program_day(user_id, day, program_id=program_id)
        if freeing_routine_id is not None:
            await db.delete_routine(freeing_routine_id)
    except Exception:
        with suppress(Exception):
            await db.delete_program_by_id(program_id)
        raise

    return {
        "program_id": program_id, "day_count": len(days),
        "name": draft["name"], "replacing": False,
    }


async def resolve_replace_target(user_id: int, draft: dict[str, Any]) -> Optional[Any]:
    """Сохранённая программа, которую этот черновик реально заменит — только
    если `replaces` называет многодневку и она всё ещё принадлежит этому
    пользователю (могли удалить между предложением и сохранением). None
    значит: сохранение заведёт новую программу, а не правку существующей —
    ровно то, что нужно и finalize_program_save, и вызывающим для выбора
    текста об ошибке (см. handlers/ai_trainer._run_program_save)."""
    replaces = draft.get("replaces")
    if replaces and replaces.get("kind") == "program":
        program = await db.get_program(replaces["id"])
        if program is not None and program["user_id"] == user_id:
            return program
    return None


async def finalize_program_save(user_id: int, draft: dict[str, Any]) -> dict[str, Any]:
    """Диспетчер путей сохранения черновика после того, как он атомарно забран
    у своего хранилища (FSM у бота, ai_program_drafts у REST).

    Правка сохранённой многодневки, которую черновик называет (`replaces`) и
    которая всё ещё принадлежит этому пользователю, идёт в
    save_into_existing_program; всё остальное (новая программа, замена
    одиночной программы) — в save_as_new_program.
    """
    program = await resolve_replace_target(user_id, draft)
    if program is not None:
        return await save_into_existing_program(user_id, draft, program)

    replaces = draft.get("replaces")
    freeing_routine_id = None
    if replaces and replaces.get("kind") == "routine":
        routine = await db.get_routine(replaces["id"])
        if routine is not None and routine["user_id"] == user_id:
            freeing_routine_id = routine["id"]

    return await save_as_new_program(user_id, draft, freeing_routine_id=freeing_routine_id)
