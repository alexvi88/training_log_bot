"""«Похоже, это дубль»: GET /v1/exercises/{id}/catalog-duplicates.

Резолвер импорта — бесплатно и первым; уверен — модель не зовётся. Не уверен —
один вызов Luna (chat/completions строгой схемой, id только из короткого
списка), ответ кэшируется на упражнение и его имя, квота
`ai_limits.KIND_MERGE_SUGGEST`. Квота, деньги или сбой — молча подсказки
резолвера. Имена — настоящие из дневника владельца.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from luna_fakes import as_luna

import ai_limits
import ai_trainer
import api_v1
import config
import exercise_duplicates
import seed_data

pytestmark = pytest.mark.asyncio

USER = 111

# Имя, которое человек набрал сам → упражнение каталога, которым оно и
# является. Резолвер ни в одном из них не уверен — решает модель, и правильный
# шаблон обязан попасть в её короткий список.
OWNER_NAMES = {
    "conventional deadlift": "Становая тяга",
    "bench press": "Жим штанги лёжа",
    "lateral raises - dumbbells": "Разведение гантелей в стороны",
    "lateral raises - cable - cuff": "Махи в кроссовере",
    "hammer curls dumbbells": "Подъём гантелей на бицепс молотом",
    "biceps curls - alternating dumbbells": "Подъём гантелей на бицепс",
    "abs - pull down block": "Скручивания в блоке",
    "tric block - 1arm - cuff": "Разгибание на трицепс в кроссовере одной рукой",
    "chest press - flat": "Жим в тренажёре",
}


class FakeModel:
    """chat/completions Luna: отвечает id шаблонов по их идентичности, взяв id
    из присланного короткого списка (как сделала бы модель)."""

    def __init__(self, answer: dict[str, str] | None = None, error: Exception | None = None):
        self.answer = answer or {}
        self.error = error
        self.create = AsyncMock(side_effect=self._create)
        self.client = as_luna(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))))
        self.enums: list[list[str]] = []

    async def _create(self, **kwargs):
        if self.error is not None:
            raise self.error
        enum = kwargs["response_format"]["json_schema"]["schema"]["properties"]["matches"]["items"]["properties"]["id"]["enum"]
        self.enums.append(enum)
        payload = json.loads(kwargs["messages"][1]["content"])
        by_name = {item["name"]: item["id"] for item in payload["catalog"]}
        lang = self.lang
        matches = [
            {"id": by_name[seed_data.localized_exercise_name(identity, lang)], "confidence": confidence}
            for identity, confidence in self.answer.items()
            if seed_data.localized_exercise_name(identity, lang) in by_name
        ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"matches": matches})))],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=30),
        )

    lang = "en"


def _install(monkeypatch, model: FakeModel) -> FakeModel:
    backend = ai_trainer.ChatBackend(
        client=model.client, model=config.OPENAI_CHAT_MODEL, effort="medium", quick_effort="none",
    )
    monkeypatch.setattr(ai_trainer, "luna_backend", lambda: backend)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    return model


async def _client(fresh_db, lang="en"):
    await fresh_db.get_or_create_user(telegram_id=USER, username="tester")
    await fresh_db.set_user_lang(USER, lang)
    code = await fresh_db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _own(fresh_db, name, group=None):
    group_id = None
    if group is not None:
        groups = await fresh_db.list_muscle_groups(USER)
        group_id = next(g["id"] for g in groups if g["name"] == group)
    return await fresh_db.create_exercise(USER, name, group_id)


async def _duplicates(client, exercise_id):
    resp = await client.get(f"/exercises/{exercise_id}/catalog-duplicates")
    assert resp.status_code == 200, resp.text
    return resp.json()["candidates"]


async def _cost_events(fresh_db):
    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM cost_events")
    return (await cur.fetchone())["n"]


# ---------- резолвер уверен: модель не зовётся ----------


@pytest.mark.parametrize("lang,name", [("en", "barbell bench press"), ("ru", "лёжа жим штанги")])
async def test_resolver_sure_answers_without_the_model(fresh_db, monkeypatch, lang, name):
    model = _install(monkeypatch, FakeModel())
    client = await _client(fresh_db, lang)
    exercise_id = await _own(fresh_db, name)
    candidates = await _duplicates(client, exercise_id)
    assert candidates[0]["original_name"] == "Жим штанги лёжа"
    assert candidates[0]["confidence"] == "sure"
    assert candidates[0]["name"] == seed_data.localized_exercise_name("Жим штанги лёжа", lang)
    assert candidates[0]["group"] == seed_data.localized_muscle_group_name("Грудь", lang)
    assert isinstance(candidates[0]["template_id"], int)
    assert len(candidates) <= exercise_duplicates.MAX_CANDIDATES
    assert all(c["confidence"] == "maybe" for c in candidates[1:])
    model.create.assert_not_awaited()
    assert await fresh_db.get_ai_usage_today(USER, ai_limits.KIND_MERGE_SUGGEST) == 0


# ---------- модель: только id из короткого списка ----------


@pytest.mark.parametrize("name,identity", list(OWNER_NAMES.items()))
async def test_owner_names_go_to_the_model_with_the_right_template_in_the_list(
    fresh_db, monkeypatch, name, identity,
):
    model = _install(monkeypatch, FakeModel({identity: "sure"}))
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, name)
    before = await _cost_events(fresh_db)
    candidates = await _duplicates(client, exercise_id)
    model.create.assert_awaited_once()
    kwargs = model.create.await_args.kwargs
    assert kwargs["extra_body"]["reasoning_effort"] == "none"
    assert kwargs["extra_body"]["prompt_cache_key"] == f"dup_suggest-{USER}"
    assert kwargs["messages"][0]["content"] == exercise_duplicates.SYSTEM_PROMPT
    enum = model.enums[0]
    assert len(enum) <= exercise_duplicates.SHORTLIST_SIZE
    templates = await fresh_db.find_global_templates_by_names([identity])
    assert str(templates[identity]["id"]) in enum, f"{identity} не попал в короткий список для {name!r}"
    assert [(c["original_name"], c["confidence"]) for c in candidates] == [(identity, "sure")]
    assert candidates[0]["name"] == seed_data.localized_exercise_name(identity, "en")
    # Цена — через paid_call, квота списана.
    assert await _cost_events(fresh_db) == before + 1
    assert await fresh_db.get_ai_usage_today(USER, ai_limits.KIND_MERGE_SUGGEST) == 1


async def test_model_answer_is_cached_until_the_name_changes(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Становая тяга": "sure", "Становая тяга сумо": "maybe"}))
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, "conventional deadlift")
    first = await _duplicates(client, exercise_id)
    second = await _duplicates(client, exercise_id)
    assert first == second
    assert [c["confidence"] for c in first] == ["sure", "maybe"]
    model.create.assert_awaited_once()
    # Завёл копию шаблона — он остаётся в ответе под именем копии, без нового вызова.
    sumo = (await fresh_db.find_global_templates_by_names(["Становая тяга сумо"]))["Становая тяга сумо"]
    fork_id = await fresh_db.fork_exercise_from_template(USER, sumo["id"])
    assert await fresh_db.update_exercise_name(fork_id, "Sumo DL")
    third = await _duplicates(client, exercise_id)
    assert [c["original_name"] for c in third] == ["Становая тяга", "Становая тяга сумо"]
    assert third[1]["name"] == "Sumo DL"
    model.create.assert_awaited_once()
    # Переименовал — спрашиваем заново.
    assert await fresh_db.update_exercise_name(exercise_id, "deadlift conventional stance")
    await _duplicates(client, exercise_id)
    assert model.create.await_count == 2


async def test_empty_model_answer_is_cached_too(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({}))
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, "tric block - 1arm - cuff")
    assert await _duplicates(client, exercise_id) == []
    assert await _duplicates(client, exercise_id) == []
    model.create.assert_awaited_once()


# ---------- отказы: молча подсказки резолвера ----------


async def test_quota_exhausted_falls_back_to_resolver_maybes(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Жим штанги лёжа": "sure"}))
    monkeypatch.setattr(config, "AI_MERGE_SUGGEST_DAILY_LIMIT", 1)
    client = await _client(fresh_db, "en")
    await fresh_db.increment_ai_usage(USER, ai_limits.KIND_MERGE_SUGGEST)
    exercise_id = await _own(fresh_db, "bench press")
    candidates = await _duplicates(client, exercise_id)
    model.create.assert_not_awaited()
    assert candidates and all(c["confidence"] == "maybe" for c in candidates)
    assert candidates[0]["original_name"] == "Жим штанги лёжа"
    # Ничего не закэшировали: завтра (новая квота) модель спросят.
    row = await fresh_db.get_exercise(exercise_id)
    assert row["dup_suggest"] is None


async def test_own_accounts_skip_the_quota(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Жим штанги лёжа": "sure"}))
    monkeypatch.setattr(config, "AI_MERGE_SUGGEST_DAILY_LIMIT", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", USER)
    client = await _client(fresh_db, "en")
    await fresh_db.increment_ai_usage(USER, ai_limits.KIND_MERGE_SUGGEST)
    exercise_id = await _own(fresh_db, "bench press")
    assert [c["confidence"] for c in await _duplicates(client, exercise_id)] == ["sure"]
    model.create.assert_awaited_once()


async def test_hard_money_stop_falls_back_silently(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Жим штанги лёжа": "sure"}))

    async def hard():
        return ai_limits.KIND_SPEND_HARD

    monkeypatch.setattr(ai_limits, "spend_level", hard)
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, "bench press")
    candidates = await _duplicates(client, exercise_id)
    model.create.assert_not_awaited()
    assert candidates[0]["confidence"] == "maybe"


async def test_without_ai_consent_the_model_is_not_asked(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Жим штанги лёжа": "sure"}))
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, "bench press")
    resp = await client.get(
        f"/exercises/{exercise_id}/catalog-duplicates", headers={config.AI_CONSENT_CLIENT_HEADER: "1"},
    )
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    model.create.assert_not_awaited()
    assert candidates and all(c["confidence"] == "maybe" for c in candidates)


async def test_model_error_falls_back_silently(fresh_db, monkeypatch):
    _install(monkeypatch, FakeModel(error=RuntimeError("boom")))
    client = await _client(fresh_db, "en")
    exercise_id = await _own(fresh_db, "hammer curls dumbbells")
    candidates = await _duplicates(client, exercise_id)
    assert candidates and all(c["confidence"] == "maybe" for c in candidates)
    assert "Подъём гантелей на бицепс молотом" in [c["original_name"] for c in candidates]
    assert (await fresh_db.get_exercise(exercise_id))["dup_suggest"] is None


# ---------- что не предлагается ----------


async def test_catalog_copy_is_left_out_and_owned_template_comes_under_its_copy_name(fresh_db, monkeypatch):
    model = _install(monkeypatch, FakeModel({"Жим штанги лёжа": "sure"}))
    client = await _client(fresh_db, "ru")
    bench = (await fresh_db.find_global_templates_by_names(["Жим штанги лёжа"]))["Жим штанги лёжа"]
    fork_id = await fresh_db.fork_exercise_from_template(USER, bench["id"])
    assert await fresh_db.update_exercise_name(fork_id, "Жим на скамье")
    # Копия каталога — пусто.
    assert await _duplicates(client, fork_id) == []
    # Своё с тем же именем, что у шаблона, чья копия уже есть, — уверенный
    # кандидат под именем копии, модель не зовётся.
    own_id = await _own(fresh_db, "barbell bench press")
    candidates = await _duplicates(client, own_id)
    assert candidates[0]["original_name"] == "Жим штанги лёжа"
    assert candidates[0]["name"] == "Жим на скамье"
    assert candidates[0]["confidence"] == "sure"
    model.create.assert_not_awaited()


async def test_owner_case_conventional_deadlift_finds_own_catalog_copy(fresh_db, monkeypatch):
    """Владелец: «Barbell Deadlift» — его копия «Становой тяги», а своё
    «conventional deadlift» модель раньше не могла сопоставить: становой не
    было в её списке. Теперь она там, кандидат — под именем копии, и
    «Добавить» отдаёт ту же копию, в неё и объединяем."""
    _install(monkeypatch, FakeModel({"Становая тяга": "sure"}))
    client = await _client(fresh_db, "en")
    deadlift = (await fresh_db.find_global_templates_by_names(["Становая тяга"]))["Становая тяга"]
    copy_id = await fresh_db.fork_exercise_from_template(USER, deadlift["id"])
    assert await fresh_db.update_exercise_name(copy_id, "Barbell Deadlift")
    own_id = await _own(fresh_db, "conventional deadlift")
    candidates = await _duplicates(client, own_id)
    assert candidates and candidates[0]["original_name"] == "Становая тяга"
    assert candidates[0]["name"] == "Barbell Deadlift"
    added = await client.post(f"/exercise-templates/{candidates[0]['template_id']}/add")
    assert added.status_code in (200, 201), added.text
    assert added.json()["id"] == copy_id


async def test_foreign_exercise_is_404(fresh_db, monkeypatch):
    _install(monkeypatch, FakeModel())
    client = await _client(fresh_db, "en")
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    other = await fresh_db.create_exercise(222, "bench press", None)
    resp = await client.get(f"/exercises/{other}/catalog-duplicates")
    assert resp.status_code == 404


# ---------- выбор: форк шаблона и объединение в него ----------


async def test_merge_into_fresh_fork_keeps_catalog_identity_and_moves_history(fresh_db, monkeypatch):
    _install(monkeypatch, FakeModel({"Становая тяга": "sure"}))
    client = await _client(fresh_db, "en")
    own_id = await _own(fresh_db, "conventional deadlift", "Спина")
    workout_id = await fresh_db.create_workout(USER)
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, own_id, 0)
    await fresh_db.add_set(block_id, own_id, 0, 0, 140, 5)
    await fresh_db.finish_workout(workout_id)

    candidate = (await _duplicates(client, own_id))[0]
    added = await client.post(f"/exercise-templates/{candidate['template_id']}/add")
    assert added.status_code in (200, 201), added.text
    target_id = added.json()["id"]
    merged = await client.post("/exercises/merge", json={"target_id": target_id, "source_id": own_id})
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["original_name"] == "Становая тяга"
    assert body["merge_id"]
    assert await fresh_db.get_exercise(own_id) is None
    row = await fresh_db.get_exercise(target_id)
    assert row["original_name"] == "Становая тяга"
    cur = await fresh_db.conn().execute("SELECT COUNT(*) AS n FROM sets WHERE exercise_id = ?", (target_id,))
    assert (await cur.fetchone())["n"] == 1
