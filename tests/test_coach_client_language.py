"""Данные атлета у тренера — на языке АТЛЕТА, хром — на языке тренера.

Тренер с английским интерфейсом составляет программы русскому атлету — и
упражнения, группы, назначенная программа, программа, собранная с AI для
атлета, должны называться так, как их видит сам атлет. Сквозной прогон всего
/v1 на разных языках — `test_api_v1_language_invariant.
test_coach_and_client_on_different_languages`; здесь — точечные правила:
копия программы у атлета, черновик AI-тренера для атлета, заметка режима,
каталог в этом режиме, тег группы в карточке тренировки бота, схемы подходов.
"""

from __future__ import annotations

import json

import pytest

import ai_program_actions
import coach_program_ai
import coach_service
import db
import formatting
import i18n
import seed_data
from handlers import workout as workout_handlers

pytestmark = pytest.mark.asyncio

COACH = 801
ATHLETE = 802
BENCH = "Жим штанги лёжа"
SQUAT = "Присед со штангой"


async def _pair(coach_lang: str, athlete_lang: str) -> None:
    for uid, lang in ((COACH, coach_lang), (ATHLETE, athlete_lang)):
        await db.get_or_create_user(telegram_id=uid, username=f"user{uid}")
        await db.set_user_lang(uid, lang)
    await db.upsert_coach_profile(COACH, "Coach Mike")
    assert await db.create_coach_link(COACH, ATHLETE, "Masha", show_bodyweight=False)
    await db.update_user(ATHLETE, ai_consent_at=db.now_iso())


async def test_relocalized_target_reads_any_language():
    assert seed_data.relocalized_target("3×30–60 sec", "ru") == "3×30–60 сек"
    assert seed_data.relocalized_target("3×30–60 сек", "en") == "3×30–60 sec"
    assert seed_data.relocalized_target("3 sets", "ru") == i18n.t_in("ru", "routine.target_sets_only", sets=3)
    assert seed_data.relocalized_target(i18n.t_in("ru", "routine.target_sets_only", sets=4), "en") == "4 sets"
    assert seed_data.relocalized_target("4×6–8", "en") == "4×6–8"
    assert seed_data.relocalized_target("sets galore", "ru") == "sets galore"
    assert seed_data.relocalized_target(None, "ru") is None


@pytest.mark.parametrize("coach_lang, athlete_lang", [("en", "ru"), ("ru", "en")])
async def test_assigned_catalog_program_lands_in_the_athletes_language(fresh_db, coach_lang, athlete_lang):
    """Каталожная программа тренера (снимок на его языке) — у атлета на языке
    атлета: имя, дни, описание, схемы; ключ каталога остаётся, и смена языка
    атлета переводит копию так же, как его собственные каталожные."""
    await _pair(coach_lang, athlete_lang)
    key = next(p["key"] for p in seed_data.WORKOUT_PROGRAMS if any(
        target.endswith(" сек") for _day, exercises in p["days"] for _name, target in exercises
    ))
    with i18n.use_lang(coach_lang):
        source = await seed_data.instantiate_program(COACH, key, seed_data.localized_program_name(key, coach_lang))
    new_id, name = await coach_service.assign_program(COACH, ATHLETE, source)

    copy = await db.get_program(new_id)
    assert name == copy["name"] == seed_data.localized_program_name(key, athlete_lang)
    assert copy["source"] == "coach" and copy["source_ref"] == key
    assert copy["description"] == db.clean_program_description(
        seed_data.localized_program_description(key, athlete_lang)
    )
    days = await db.list_program_days_by_id(new_id)
    assert [d["name"] for d in days] == [
        seed_data.localized_program_day_name(key, i, athlete_lang) for i in range(len(days))
    ]
    seconds = i18n.t_in(athlete_lang, "program.target.seconds")
    targets = [r["target"] for d in days for r in await db.list_routine_exercises(d["id"]) if r["target"]]
    assert any(t.endswith(f" {seconds}") for t in targets), targets
    wrong = i18n.t_in(coach_lang, "program.target.seconds")
    assert not any(t.endswith(f" {wrong}") for t in targets), targets

    # Атлет сменил язык — копия от тренера переводится вместе с его каталожными.
    await db.set_user_lang(ATHLETE, coach_lang)
    assert (await db.get_program(new_id))["name"] == seed_data.localized_program_name(key, coach_lang)


async def test_assigned_custom_program_keeps_typed_names(fresh_db):
    """Своё имя программы и дня, набранное тренером, — как набрано; упражнения
    — форки атлета на его языке, схема «3 sets» — на его языке."""
    await _pair("en", "ru")
    source = await db.create_program(COACH, "Strength block")
    day = await db.create_routine(COACH, "Heavy day", program_id=source)
    with i18n.use_lang("en"):
        bench = await db.get_or_create_user_exercise_by_name(COACH, BENCH)
        await db.add_routine_exercise(day, bench, 0, formatting.build_routine_target(3, None, None))
    new_id, name = await coach_service.assign_program(COACH, ATHLETE, source)
    assert name == "Strength block"
    [copy_day] = await db.list_program_days_by_id(new_id)
    assert copy_day["name"] == "Heavy day"
    [row] = await db.list_routine_exercises(copy_day["id"])
    assert row["display_name"] == BENCH
    assert row["target"] == i18n.t_in("ru", "routine.target_sets_only", sets=3)


async def test_client_chat_note_names_the_athletes_language_only_when_it_differs(fresh_db):
    await _pair("en", "ru")
    chat = await coach_program_ai.open_chat(COACH, ATHLETE)
    assert chat.lang == "ru"
    with i18n.use_lang("en"):
        note = chat.system_note()
    assert note.endswith(i18n.t_in("en", "coach.ai_chat.mode_lang.ru"))
    with i18n.use_lang("ru"):
        assert chat.system_note() == i18n.t("coach.ai_chat.mode_prompt", name="Masha", unit="kg")


async def test_client_chat_catalog_is_in_the_athletes_language(fresh_db):
    await _pair("en", "ru")
    chat = await coach_program_ai.open_chat(COACH, ATHLETE)
    with i18n.use_lang("en"):
        payload = json.loads(await chat.execute("list_exercise_catalog", {}))
    names = {name for names in payload["catalog"].values() for name in names}
    assert BENCH in names and "Barbell Bench Press" not in names


@pytest.mark.parametrize("coach_lang, athlete_lang", [("en", "ru"), ("ru", "en")])
async def test_client_chat_draft_and_saved_program_in_the_athletes_language(fresh_db, coach_lang, athlete_lang):
    """Черновик программы атлету: каталожные упражнения и схемы — на языке
    атлета, по идентичности (своё упражнение тренера под каталожным именем —
    тоже). Сохраняется тренеру его же упражнениями, с прогрессией; копия у
    атлета после «Назначить» — его упражнения на его языке."""
    await _pair(coach_lang, athlete_lang)
    with i18n.use_lang(coach_lang):
        own_bench = await db.get_or_create_user_exercise_by_name(COACH, BENCH)
    chat = await coach_program_ai.open_chat(COACH, ATHLETE)
    drafts = []

    async def on_program(draft):
        drafts.append(draft)

    with i18n.use_lang(coach_lang):
        await chat.execute("propose_program", {
            "name": "P", "description": "D",
            "days": [{"name": "A", "exercises": [
                {"name": seed_data.localized_exercise_name(BENCH, coach_lang), "sets": 3, "reps_min": 5,
                 "reps_max": 8, "progression": {"type": "linear"}},
                {"name": SQUAT, "sets": 3},
            ]}],
        }, on_program=on_program)
    [draft] = drafts
    items = draft["days"][0]["items"]
    assert [i["name"] for i in items] == [
        seed_data.localized_exercise_name(BENCH, athlete_lang), seed_data.localized_exercise_name(SQUAT, athlete_lang),
    ]
    assert items[1]["target"] == i18n.t_in(athlete_lang, "routine.target_sets_only", sets=3)

    # Прогрессия, которую модель поставила упражнению, переживает сохранение
    # тренеру, хотя имя в черновике — на языке атлета.
    items[0]["progression"] = {"kind": "double", "step": 2.5}
    program_id = await db.create_program(COACH, "P")
    routine_id = await ai_program_actions.create_program_day(COACH, draft["days"][0], program_id)
    rows = await db.list_routine_exercises(routine_id)
    assert rows[0]["exercise_id"] == own_bench, "у тренера — его же упражнение"
    assert rows[0]["display_name"] == seed_data.localized_exercise_name(BENCH, coach_lang)
    assert rows[0]["progression"] and json.loads(rows[0]["progression"]) == {"kind": "double", "step": 2.5}

    new_id, _ = await coach_service.assign_program(COACH, ATHLETE, program_id)
    [day] = await db.list_program_days_by_id(new_id)
    shown = [r["display_name"] for r in await db.list_routine_exercises(day["id"])]
    assert shown == [
        seed_data.localized_exercise_name(BENCH, athlete_lang), seed_data.localized_exercise_name(SQUAT, athlete_lang),
    ]


async def test_bot_workout_card_tags_the_athletes_group_in_their_language(fresh_db):
    """Карточка тренировки атлета у тренера в боте: имя упражнения и тег
    группы — на языке атлета, подписи карточки — на языке тренера."""
    await _pair("en", "ru")
    with i18n.use_lang("ru"):
        bench = await db.get_or_create_user_exercise_by_name(ATHLETE, BENCH)
    wid = await db.create_workout(ATHLETE)
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, bench, 0)
    await db.add_set(block, bench, 1, 0, 80.0, 5)
    await db.finish_workout(wid)
    athlete = await db.get_user(ATHLETE)
    workout = await db.get_workout(wid)
    with i18n.use_lang("en"):
        summary_fn, _tonnage, _ = await workout_handlers._finished_summary(
            workout, athlete, None, data_lang=coach_service.client_lang(athlete)
        )
        text = summary_fn(None)
    group = (await db.get_exercise(bench))["primary_group_id"]
    group_name = next(g["name"] for g in await db.list_muscle_groups(ATHLETE) if g["id"] == group)
    assert f"{BENCH} [{group_name.upper()}]" in text, text
    assert "Barbell Bench Press" not in text
