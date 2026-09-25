"""Язык атлета в REST `/v1` — точечные проверки к каждой найденной протечке.

Общий инвариант «ни одного слова чужого языка» для всех маршрутов разом —
tests/test_api_v1_language_invariant.py; тут — каждый механизм отдельно, чтобы
падение сразу называло, что именно сломалось: язык нового app-only аккаунта,
язык хода тренера, перевод копий из каталога при смене языка, человеческий
текст ошибок.
"""

from __future__ import annotations

import ast
import pathlib

import httpx
import pytest

import ai_trainer
import api_v1
import api_v1_common
import i18n
import seed_data

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked(fresh_db, lang: str, telegram_id: int = 111) -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _apple(monkeypatch, apple_user_id: str = "apple-lang"):
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=apple_user_id, email=None),
    )


# ---------- язык нового app-only аккаунта (/auth/apple) ----------


@pytest.mark.parametrize(
    ("body_lang", "header", "expected"),
    [
        ("en", None, "en"),
        ("en-US", None, "en"),
        ("ru", "en-US,en;q=0.9", "ru"),  # поле тела важнее заголовка
        (None, "en-GB,en;q=0.8", "en"),  # без поля — Accept-Language
        (None, "de-DE;q=0.4, uk;q=0.9", "ru"),  # вес q, а не порядок
        (None, None, "ru"),  # ничего — прежний дефолт
    ],
)
async def test_auth_apple_new_account_takes_the_device_language(fresh_db, monkeypatch, body_lang, header, expected):
    _apple(monkeypatch)
    body = {"identity_token": "t"}
    if body_lang is not None:
        body["lang"] = body_lang
    headers = {"Accept-Language": header} if header else {}
    resp = await _client().post("/auth/apple", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["lang"] == expected
    assert (await fresh_db.get_user(resp.json()["user_id"]))["lang"] == expected


async def test_auth_apple_never_overwrites_an_existing_users_language(fresh_db, monkeypatch):
    _apple(monkeypatch, "apple-existing")
    first = await _client().post("/auth/apple", json={"identity_token": "t", "lang": "en"})
    user_id = first.json()["user_id"]
    await fresh_db.set_user_lang(user_id, "ru")  # человек сам переключил
    again = await _client().post("/auth/apple", json={"identity_token": "t2", "lang": "en"})
    assert again.json()["user_id"] == user_id
    assert (await fresh_db.get_user(user_id))["lang"] == "ru"


async def test_auth_apple_link_code_path_keeps_the_telegram_users_language(fresh_db, monkeypatch):
    _apple(monkeypatch, "apple-link")
    await fresh_db.get_or_create_user(telegram_id=222, username="tg")
    code = await fresh_db.issue_oauth_link_code(222, ttl_seconds=600, digits=8)
    resp = await _client().post("/auth/apple", json={"identity_token": "t", "link_code": code, "lang": "en"})
    assert resp.status_code == 200
    assert (await fresh_db.get_user(222))["lang"] == "ru"


# ---------- единицы нового app-only аккаунта (/auth/apple) ----------


@pytest.mark.parametrize(
    ("body_unit", "expected"),
    [
        ("lb", "lb"),
        ("LB", "lb"),
        ("kg", "kg"),
        ("stone", "kg"),  # мусор — молча прежний дефолт, вход не срывается
        (5, "kg"),
        (None, "kg"),  # старая сборка без поля
    ],
)
async def test_auth_apple_new_account_takes_the_device_unit(fresh_db, monkeypatch, body_unit, expected):
    _apple(monkeypatch, "apple-unit")
    body = {"identity_token": "t"}
    if body_unit is not None:
        body["unit"] = body_unit
    resp = await _client().post("/auth/apple", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unit"] == expected
    assert (await fresh_db.get_user(resp.json()["user_id"]))["unit"] == expected


async def test_auth_apple_never_overwrites_an_existing_users_unit(fresh_db, monkeypatch):
    _apple(monkeypatch, "apple-unit-existing")
    first = await _client().post("/auth/apple", json={"identity_token": "t"})
    user_id = first.json()["user_id"]
    assert first.json()["unit"] == "kg"
    again = await _client().post("/auth/apple", json={"identity_token": "t2", "unit": "lb"})
    assert again.json()["user_id"] == user_id
    assert again.json()["unit"] == "kg"
    assert (await fresh_db.get_user(user_id))["unit"] == "kg"


async def test_auth_apple_link_code_path_keeps_the_telegram_users_unit(fresh_db, monkeypatch):
    _apple(monkeypatch, "apple-unit-link")
    await fresh_db.get_or_create_user(telegram_id=223, username="tg")
    code = await fresh_db.issue_oauth_link_code(223, ttl_seconds=600, digits=8)
    resp = await _client().post("/auth/apple", json={"identity_token": "t", "link_code": code, "unit": "lb"})
    assert resp.status_code == 200
    assert (await fresh_db.get_user(223))["unit"] == "kg"


async def test_me_carries_the_app_store_url_only_when_configured(fresh_db, monkeypatch):
    import config

    client = await _linked(fresh_db, "en")
    assert (await client.get("/me")).json()["app_store_url"] is None
    monkeypatch.setattr(config, "APP_STORE_URL", "https://apps.apple.com/app/id123")
    assert (await client.get("/me")).json()["app_store_url"] == "https://apps.apple.com/app/id123"


# ---------- язык на весь запрос ----------


async def test_authenticated_request_runs_under_the_users_language(fresh_db):
    """authed_user_id выставляет язык на весь обработчик — и он не утекает
    наружу, в вызывающий контекст (тест сам живёт на ru)."""
    client = await _linked(fresh_db, "en")
    with i18n.use_lang("ru"):
        resp = await client.get("/exercises/999999/media")
        assert i18n.get_lang() == "ru"
    assert resp.status_code == 404
    assert resp.json()["message"] == i18n.t_in("en", "api.error.not_found")


# ---------- тренер: язык хода ----------


async def test_coach_turn_runs_under_the_users_language(fresh_db, monkeypatch):
    """_run_turn: и системный промпт, и подпись кнопки отката — на языке
    атлета, а не русские по умолчанию ContextVar."""
    seen = {}

    async def fake_ask(user_id, question, history, on_action=None, on_wire=None, **kwargs):
        seen["lang"] = i18n.get_lang()
        seen["prompt"] = ai_trainer._system_prompt()
        await ai_trainer.execute_tool(
            user_id, "create_exercise", {"name": "Pallof press", "group": "Chest"}, on_action=on_action
        )
        return "Done."

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked(fresh_db, "en")
    with i18n.use_lang("ru"):  # контекст нарочно «не тот»
        resp = await client.post("/ai/ask", json={"question": "Add a Pallof press"})
    assert resp.status_code == 200, resp.text
    assert seen["lang"] == "en"
    assert seen["prompt"].endswith(i18n.t_in("en", "ai.language_tail"))
    assert "Отвечай ТОЛЬКО по-русски" not in seen["prompt"]
    label = resp.json()["actions"][0]["label"]
    assert label == i18n.t_in("en", "ai.action.remove_created", name="Pallof press")


async def test_video_context_block_is_built_in_the_users_language(fresh_db):
    import video_analysis

    analysis = {"exercise": "squat", "exercise_confidence": "high", "view": {}, "observations": [
        {"what": "knees cave", "severity": "blocking", "confidence": "low"}]}
    with i18n.use_lang("en"):
        block = video_analysis.to_context_block(analysis)
    assert f"(уверенность {i18n.t_in('en', 'ai.video.confidence_high')})" in block
    assert f"(уверенность {i18n.t_in('ru', 'ai.video.confidence_high')})" not in block


async def test_factcheck_and_food_run_under_the_users_language(fresh_db, monkeypatch):
    seen = []

    async def fake_fact(user_id, text, image_data_url=None):
        seen.append(("fact", i18n.get_lang()))
        return "ok"

    async def fake_food(user_id, **kwargs):
        seen.append(("food", i18n.get_lang()))
        return {"is_food": True, "description": "Rice", "items": [], "calories": 1, "protein": 0,
                "fat": 0, "carbs": 0}

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "fact_check_post", fake_fact)
    monkeypatch.setattr(ai_trainer, "analyze_food", fake_food)
    client = await _linked(fresh_db, "en")
    with i18n.use_lang("ru"):
        assert (await client.post("/factcheck", json={"text": "x"})).status_code == 200
        assert (await client.post("/food/parse", json={"text": "rice"})).status_code == 200
    assert seen == [("fact", "en"), ("food", "en")]


async def test_comment_on_missing_workout_speaks_the_users_language(fresh_db, user_id):
    await fresh_db.set_user_lang(user_id, "en")
    with i18n.use_lang("ru"):
        text = await ai_trainer.comment_on_workout(user_id, 999999)
    assert text == i18n.t_in("en", "ai.comment.workout_not_found")


# ---------- группы мышц ----------


async def test_muscle_groups_are_localized_and_english_names_map_back(fresh_db):
    client = await _linked(fresh_db, "en")
    groups = (await client.get("/muscle-groups")).json()
    names = {g["name"] for g in groups}
    assert "Chest" in names and "Грудь" not in names

    chest = next(g for g in groups if g["name"] == "Chest")
    # Имя встроенной группы, присланное обратно, — та же группа, а не новая.
    again = await client.post("/muscle-groups", json={"name": "chest"})
    assert again.status_code == 200
    assert again.json()["id"] == chest["id"] and again.json()["name"] == "Chest"
    ru_name = await client.post("/muscle-groups", json={"name": "Грудь"})
    assert ru_name.json()["id"] == chest["id"]
    assert len((await client.get("/muscle-groups")).json()) == len(groups)

    own = await client.post("/muscle-groups", json={"name": "Forearms"})
    assert own.status_code == 201 and own.json()["name"] == "Forearms"


# ---------- описание техники, шаблоны ----------


async def _forked(client, query: str) -> dict:
    template = (await client.get(f"/exercise-templates?query={query}")).json()[0]
    return (await client.post(f"/exercise-templates/{template['id']}/add")).json()


async def test_description_defaults_to_the_users_language(fresh_db):
    client = await _linked(fresh_db, "en")
    ex = await _forked(client, "bench")
    text = (await client.get(f"/exercises/{ex['id']}/description")).json()["description"]
    assert not any("а" <= c.lower() <= "я" for c in text)
    explicit = (await client.get(f"/exercises/{ex['id']}/description?lang=ru")).json()["description"]
    assert any("а" <= c.lower() <= "я" for c in explicit)


async def test_exercise_json_reports_has_description(fresh_db):
    client = await _linked(fresh_db, "en")
    catalog = await _forked(client, "bench")
    assert catalog["has_description"] is True  # встроенное описание из каталога
    own = (await client.post("/exercises", json={"name": "Odd lift"})).json()
    assert own["has_description"] is False
    patched = (await client.patch(f"/exercises/{own['id']}", json={"description": "Slow."})).json()
    assert patched["has_description"] is True
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert listed[catalog["id"]]["has_description"] is True


async def test_template_search_limit_goes_up_to_200(fresh_db, monkeypatch):
    import db as db_module

    seen = {}
    real = db_module.search_exercise_templates

    async def spy(user_id, query, limit=8):
        seen["limit"] = limit
        return await real(user_id, query, limit=limit)

    monkeypatch.setattr(db_module, "search_exercise_templates", spy)
    client = await _linked(fresh_db, "ru")
    assert (await client.get("/exercise-templates?query=жим&limit=200")).status_code == 200
    assert seen["limit"] == 200
    await client.get("/exercise-templates?query=жим&limit=5000")
    assert seen["limit"] == 200


# ---------- шаринг ----------


async def test_share_preview_localizes_group_and_catalog_names_for_the_viewer(fresh_db):
    owner = await _linked(fresh_db, "ru", telegram_id=111)
    ex = await _forked(owner, "жим штанги")
    token = (await owner.post(f"/share/exercises/{ex['id']}")).json()["token"]

    viewer = await _linked(fresh_db, "en", telegram_id=222)
    preview = (await viewer.get(f"/share/{token}")).json()["exercise"]
    assert preview["name"] == seed_data.localized_exercise_name(ex["original_name"], "en")
    assert preview["group"] == "Chest"


# ---------- копии из каталога при смене языка ----------


async def test_language_switch_relocalizes_untouched_catalog_copies(fresh_db, user_id):
    db = fresh_db
    template = next(t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа")
    forked = await db.fork_exercise_from_template(user_id, template["id"])
    squat = next(t for t in await db.list_all_exercise_templates() if t["name"] == "Присед со штангой")
    renamed = await db.fork_exercise_from_template(user_id, squat["id"])
    await db.update_exercise_name(renamed, "Мой присед")
    own = await db.create_exercise(user_id, "Своё упражнение", None)

    with i18n.use_lang("ru"):
        program_id = await seed_data.instantiate_program(
            user_id, "fullbody2", seed_data.localized_program_name("fullbody2", "ru")
        )

    await db.set_user_lang(user_id, "en")

    assert (await db.get_exercise(forked))["display_name"] == "Barbell Bench Press"
    assert (await db.get_exercise(forked))["original_name"] == "Жим штанги лёжа"
    assert (await db.get_exercise(renamed))["display_name"] == "Мой присед"  # своё имя не трогаем
    assert (await db.get_exercise(own))["display_name"] == "Своё упражнение"

    program = await db.get_program(program_id)
    assert program["name"] == seed_data.localized_program_name("fullbody2", "en")
    assert program["description"] == db.clean_program_description(
        seed_data.localized_program_description("fullbody2", "en")
    )
    days = await db.list_program_days_by_id(program_id)
    assert [d["name"] for d in days] == [
        seed_data.localized_program_day_name("fullbody2", i, "en") for i in range(len(days))
    ]
    targets = [
        re["target"] for d in days for re in await db.list_routine_exercises(d["id"]) if re["target"]
    ]
    assert not any("сек" in t for t in targets)
    assert any(t.endswith(i18n.t_in("en", "program.target.seconds")) for t in targets)

    # И обратно — тем же путём.
    await db.set_user_lang(user_id, "ru")
    assert (await db.get_exercise(forked))["display_name"] == "Жим штанги лёжа"
    assert (await db.get_program(program_id))["name"] == seed_data.localized_program_name("fullbody2", "ru")


async def test_relocalize_skips_a_name_already_taken(fresh_db, user_id):
    db = fresh_db
    template = next(t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа")
    forked = await db.fork_exercise_from_template(user_id, template["id"])
    clash = await db.create_exercise(user_id, "Barbell Bench Press", None)
    await db.set_user_lang(user_id, "en")
    assert (await db.get_exercise(forked))["display_name"] == "Жим штанги лёжа"
    assert (await db.get_exercise(clash))["display_name"] == "Barbell Bench Press"


async def test_renamed_catalog_program_keeps_its_name(fresh_db, user_id):
    db = fresh_db
    with i18n.use_lang("ru"):
        program_id = await seed_data.instantiate_program(user_id, "fullbody2", "Моя программа")
    await db.set_user_lang(user_id, "en")
    assert (await db.get_program(program_id))["name"] == "Моя программа"
    # Дни — из каталога, их переводим и у переименованной программы.
    days = await db.list_program_days_by_id(program_id)
    assert days[0]["name"] == seed_data.localized_program_day_name("fullbody2", 0, "en")


async def test_rest_settings_language_switch_relocalizes_exercises(fresh_db):
    client = await _linked(fresh_db, "ru")
    ex = await _forked(client, "жим штанги")
    assert (await client.patch("/settings", json={"lang": "en"})).status_code == 200
    listed = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert listed[ex["id"]]["display_name"] == seed_data.localized_exercise_name(ex["original_name"], "en")


def test_language_is_only_written_through_set_user_lang():
    """Перевод копий живёт внутри db.set_user_lang, поэтому писать users.lang
    в обход него нельзя — иначе смена языка оставит русский список."""
    offenders = []
    for path in ROOT.glob("**/*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "SET lang" in source and path.name != "db.py":
            offenders.append(str(path))
        if "update_user(" in source and "lang=" in source:
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "update_user"
                    and any(k.arg == "lang" for k in node.keywords)
                ):
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders


# ---------- ошибки: человеческий текст на языке атлета ----------


def _api_error_calls():
    for path in sorted(ROOT.glob("api_v1*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ApiError":
                yield path.name, node


def test_every_error_code_has_a_human_text():
    """Каждый ApiError без готового `human=`/`key=` получает текст по своему
    коду; код без текста в каталоге ушёл бы атлету общей фразой — это
    допустимо только осознанно, поэтому новый код обязан завести
    `api.error.<code>` (или строку в ERROR_KEY_BY_CODE)."""
    missing = []
    for filename, node in _api_error_calls():
        kwargs = {k.arg for k in node.keywords}
        if kwargs & {"human", "key"}:
            continue
        code = node.args[1] if len(node.args) > 1 else None
        if not isinstance(code, ast.Constant):
            continue  # код приходит переменной — проверяется по месту
        if api_v1_common.error_key_for_code(code.value) is None:
            missing.append(f"{filename}:{node.lineno} {code.value}")
    assert not missing, "нет человеческого текста для кодов ошибок:\n" + "\n".join(missing)


def test_error_keys_exist_in_both_catalogs():
    ru = i18n._load_catalog("ru")
    en = i18n._load_catalog("en")
    for key in set(api_v1_common.ERROR_KEY_BY_CODE.values()) | {"api.error.default"}:
        assert key in ru and key in en, key
    for filename, node in _api_error_calls():
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                assert kw.value.value in ru and kw.value.value in en, f"{filename}:{node.lineno}"


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_common_errors_speak_the_users_language(fresh_db, monkeypatch, lang):
    import config

    other = "en" if lang == "ru" else "ru"
    client = await _linked(fresh_db, lang)

    async def expect(resp, code, key=None, **params):
        body = resp.json()
        assert body["error"] == code, body
        if key is not None:
            assert body["message"] == i18n.t_in(lang, key, **params), body
            assert body["message"] != i18n.t_in(other, key, **params)
        assert body["detail"]  # машинный текст для разработчика никуда не делся

    await expect(await client.get("/workouts/999999"), "not_found", "api.error.not_found")
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    ex = (await client.post("/exercises", json={"name": "Lift" if lang == "en" else "Тяга"})).json()["id"]
    await expect(
        await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": -1, "reps": 5}),
        "bad_request", "api.error.weight_negative",
    )
    await expect(
        await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 10, "reps": 0}),
        "bad_request", "input.reps_zero",
    )
    await expect(
        await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 10, "reps": 5, "rpe": 42}),
        "bad_request", "input.rpe_range",
    )
    await expect(await client.post("/programs", json={"name": " "}), "bad_request", "api.error.name_empty")
    await expect(await client.post("/workouts/backfill", json={"date": "2999-01-01"}), "bad_request",
                 "input.date_in_future")
    await expect(await client.post("/workouts/backfill", json={"date": "nope"}), "bad_request",
                 "api.error.bad_request")

    # Лимит вопросов тренеру — тот же текст, что у бота.
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    await fresh_db.try_increment_ai_question_count(111, 1)
    await expect(await client.post("/ai/ask", json={"question": "?"}), "question_limit_exceeded",
                 "limit.question")

    # До входа — язык из Accept-Language.
    header = {"Accept-Language": "en-US" if lang == "en" else "ru-RU"}
    anon = _client()
    body = (await anon.post("/auth/link", json={"code": "0"}, headers=header)).json()
    assert body["error"] == "invalid_code"
    assert body["message"] == i18n.t_in(lang, "oauth.error_bad_code")
    body = (await anon.get("/me", headers=header)).json()
    assert body["message"] == i18n.t_in(lang, "api.error.unauthorized")
