"""Атлет, чьи упражнения форкнулись из каталога при lang=ru, переключил язык
на английский (так живёт аккаунт прогона скриншотов: RU-прогон, потом EN на
том же аккаунте).

Список упражнений после db.set_user_lang уже английский (relocalize_catalog_
copies), но тренер продолжал называть данные по-русски: группы мышц и каталог
шаблонов в инструментах отдавались сырыми идентичностями («Грудь», «Жим
штанги стоя»), а карточки-ссылки под ответом находили русский шаблон рядом
со своим «Barbell Bench Press» — «Жим штанги лёжа, open exercise» в
английском приложении. Сверка — по идентичности, показ — на языке атлета.
"""

import json
import re

import httpx
import pytest

import ai_trainer
import api_v1
import config
import exercise_mentions
import i18n
import review_demo

pytestmark = pytest.mark.asyncio

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

# Инструкции модели, а не данные атлета: ai_trainer разговаривает с моделью
# по-русски (модуль в i18n_coverage.TODO), язык ответа держит языковой хвост.
_MODEL_FACING_KEYS = {"note", "error"}
# Идентичность шаблона — русская навсегда по замыслу, клиент её не показывает
# (тот же EN_IDENTITY_KEYS, что в tests/test_api_v1_language_invariant.py).
_IDENTITY_KEYS = {"original_name"}

READ_TOOLS = [
    ("get_training_overview", {}),
    ("get_muscle_recovery", {}),
    ("get_weekly_volume_by_group", {}),
    ("get_exercise_progress", {"exercise_name": "Barbell Bench Press"}),
    ("list_exercise_catalog", {}),
    ("get_stalled_lifts", {}),
    ("compare_periods", {}),
    ("list_recent_workouts", {}),
    ("get_full_workout_history", {}),
]

# То, что тренер повторяет из русской истории разговора после смены языка
# (кадр 64 прогона): имена своих упражнений в их прежнем, русском виде.
STALE_ANSWER = (
    "Here's the straight plan for your Жим штанги лёжа. "
    "You already have Жим штанги стоя + row/pull-ups."
)


def _cyrillic(payload, path=""):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _MODEL_FACING_KEYS or key in _IDENTITY_KEYS:
                continue
            if CYRILLIC.search(str(key)):
                yield f"{path}/<key {key}>"
            yield from _cyrillic(value, f"{path}/{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            yield from _cyrillic(value, f"{path}[{i}]")
    elif isinstance(payload, str) and CYRILLIC.search(payload):
        yield f"{path} = {payload!r}"


async def _forked_in_ru_then_en(db, user_id) -> dict[str, int]:
    user = await db.get_user(user_id)
    assert user["lang"] == "ru"
    await review_demo.seed_history(user_id)
    await db.set_user_lang(user_id, "en")
    return {ex["original_name"]: ex["id"] for ex in await db.list_user_exercises(user_id)}


async def test_the_forks_themselves_are_english_after_the_switch(fresh_db, user_id):
    own = await _forked_in_ru_then_en(fresh_db, user_id)
    rows = await fresh_db.list_user_exercises(user_id)
    assert len(rows) == len(own) == 6
    for ex in rows:
        assert not CYRILLIC.search(ex["display_name"]), ex["display_name"]
        # Идентичность не переводится никогда.
        assert CYRILLIC.search(ex["original_name"])


@pytest.mark.parametrize("tool,tool_input", READ_TOOLS)
async def test_coach_tools_name_data_in_english(fresh_db, user_id, tool, tool_input):
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        payload = json.loads(await ai_trainer.execute_tool(user_id, tool, tool_input))
    leaks = list(_cyrillic(payload))
    assert not leaks, f"{tool}: " + "; ".join(leaks[:5])


async def test_overview_names_the_exercises_and_groups_as_the_athlete_sees_them(fresh_db, user_id):
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        overview = await ai_trainer._training_overview(user_id)
    by_name = {e["name"]: e["muscle_group"] for e in overview["exercises"]}
    assert by_name["Barbell Bench Press"] == "Chest"
    assert by_name["Standing Barbell Overhead Press"] == "Shoulders"


async def test_the_english_catalog_name_still_resolves_for_a_program(fresh_db, user_id):
    """Каталог теперь уходит модели по-английски — английское имя из него
    обязано ложиться в программу, а не в unresolved."""
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        catalog = json.loads(await ai_trainer.execute_tool(user_id, "list_exercise_catalog", {}))["catalog"]
        name = catalog["Chest"][3]
        result = json.loads(await ai_trainer.execute_tool(user_id, "propose_program", {
            "name": "Push", "days": [{"name": "Day 1", "exercises": [
                {"name": name, "sets": 3, "reps_min": 8, "reps_max": 12},
                {"name": "Barbell Bench Press", "sets": 3, "reps_min": 5, "reps_max": 8},
            ]}],
        }))
    assert not result.get("unresolved"), result


async def test_russian_catalog_names_are_still_accepted_back(fresh_db, user_id):
    """Сверка по идентичности: группа, названная по-русски (из старой истории),
    находится так же, как английская."""
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        chest_en = await ai_trainer._resolve_group_id(user_id, "Chest")
        chest_ru = await ai_trainer._resolve_group_id(user_id, "Грудь")
    assert chest_en is not None and chest_en == chest_ru


async def test_stale_russian_names_in_the_answer_link_to_the_athletes_own_exercises(fresh_db, user_id):
    own = await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        found = await exercise_mentions.find_in_text(
            user_id, STALE_ANSWER, limit=exercise_mentions.MAX_MENTIONS_TOTAL
        )
    assert [ex["id"] for ex in found] == [own["Жим штанги лёжа"], own["Жим штанги стоя"], own["Подтягивания"]]
    assert [ex["display_name"] for ex in found] == [
        "Barbell Bench Press", "Standing Barbell Overhead Press", "Pull-Ups",
    ]
    assert not any(ex["is_template"] for ex in found)


async def test_catalog_template_links_are_in_the_athletes_language(fresh_db, user_id):
    """Шаблон, которого у атлета нет, — тоже ссылка, но с английским именем, и
    находится он по английскому имени, которым его назвал тренер."""
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        found = await exercise_mentions.find_in_text(user_id, "Add some Dumbbell Bench Press after it.")
    assert [(ex["display_name"], bool(ex["is_template"])) for ex in found] == [("Dumbbell Bench Press", True)]


async def test_here_is_the_plan_is_not_a_plank(fresh_db, user_id):
    await _forked_in_ru_then_en(fresh_db, user_id)
    with i18n.use_lang("en"):
        assert await exercise_mentions.find_in_text(user_id, "Here's the plan for next week.") == []


async def test_russian_athlete_still_gets_russian_links(fresh_db, user_id):
    await review_demo.seed_history(user_id)
    with i18n.use_lang("ru"):
        found = await exercise_mentions.find_in_text(
            user_id, "Жим штанги лёжа держи, добавь жим гантелей лёжа.", limit=6
        )
    assert [(ex["display_name"], bool(ex["is_template"])) for ex in found] == [
        ("Жим штанги лёжа", False), ("Жим гантелей лёжа", True),
    ]


async def test_v1_ask_mentions_after_switching_language_in_settings(fresh_db, user_id, monkeypatch):
    """Сквозь /v1, как в приложении: язык меняется PATCH /settings, ответ тренера
    повторяет русские имена из прошлой истории — чипы под ответом английские и
    ведут на свои упражнения атлета."""
    await review_demo.seed_history(user_id)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "ADMIN_ID", 1)

    async def fake_ask(uid, question, history, on_wire=None, **kwargs):
        if on_wire is not None:
            await on_wire(history + [{"role": "user", "content": question},
                                     {"role": "assistant", "content": STALE_ANSWER}])
        return STALE_ANSWER

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    code = await fresh_db.issue_oauth_link_code(user_id, ttl_seconds=600, digits=8)
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    assert (await client.patch("/settings", json={"lang": "en"})).status_code == 200

    exercises = (await client.get("/exercises")).json()
    assert not list(_cyrillic(exercises))

    resp = await client.post("/ai/ask", json={"question": "How do I progress on bench press?"})
    assert resp.status_code == 200, resp.text
    chips = resp.json()["mentions"]["exercises"]
    assert [c["display_name"] for c in chips] == [
        "Barbell Bench Press", "Standing Barbell Overhead Press", "Pull-Ups",
    ]
    assert not any(c["is_template"] for c in chips)
