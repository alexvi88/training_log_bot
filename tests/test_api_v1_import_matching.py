"""Предпросмотр и настоящий импорт (`/v1/import/csv[/preview]`) резолвят имена
упражнений ОДНИМ путём (handlers.csv_import.match_exercise_names), и этот путь
находит очевидное без модели.

Случай из жизни: у атлета «Жим штанги лёжа» с историей, а в файле (и в
подсказке экрана импорта) — «Жим лёжа». Предпросмотр предлагал «Завести
новое», а импорт потом либо уводил подходы через модель, либо заводил второе
упражнение — история раскалывалась надвое.
"""

import httpx
import pytest

import ai_trainer
import api_v1

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


async def _client(fresh_db, telegram_id=111, lang=None):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang:
        await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _fork(db, user_id, template_name):
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (template_name,)
    )
    return await db.fork_exercise_from_template(user_id, (await cur.fetchone())["id"])


def _csv(name):
    return f"date,exercise,weight,reps\n2024-01-03,{name},80,8\n2024-01-03,{name},80,8\n"


async def _count(db, sql, *params):
    cur = await db.conn().execute(sql, params)
    return (await cur.fetchone())[0]


async def _identity_count(db, user_id, identity):
    return await _count(
        db, "SELECT COUNT(*) FROM exercises WHERE user_id = ? AND is_template = 0 AND original_name = ?",
        user_id, identity,
    )


async def _exercise_count(db, user_id):
    return await _count(db, "SELECT COUNT(*) FROM exercises WHERE user_id = ? AND is_template = 0", user_id)


async def _preview_entry(client, name):
    resp = await client.post("/import/csv/preview", json={"csv": _csv(name)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    [status] = [e for e in body["exercises"] if e["name"] == name]
    [unknown] = [u for u in body["unrecognized_exercises"] if u["name"] == name] or [None]
    return status, unknown


@pytest.mark.parametrize("file_name", ["Лёжа жим штанги", "Жим лёжа (штанга)"])
async def test_same_words_go_into_existing_bench_in_preview_and_commit(fresh_db, file_name):
    """Те же слова в другом порядке или в скобках — то же упражнение, без вопросов."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    before = await _exercise_count(fresh_db, 111)

    status, unknown = await _preview_entry(client, file_name)
    assert status == {"name": file_name, "status": "existing", "exercise_id": bench, "needs_choice": False}
    assert unknown["suggested_exercise_id"] == bench
    assert unknown["needs_choice"] is False
    assert unknown["candidates"][0] == {"exercise_id": bench, "name": "Жим штанги лёжа"}

    # Старый клиент без выбора — импорт делает то, что показал предпросмотр.
    resp = await client.post("/import/csv", json={"csv": _csv(file_name)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 1
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 2
    assert await _exercise_count(fresh_db, 111) == before
    assert await _identity_count(fresh_db, 111, "Жим штанги лёжа") == 1


@pytest.mark.parametrize("file_name", ["Жим лёжа", "жим лежа"])
async def test_shorter_name_is_a_choice_not_a_guess(fresh_db, file_name):
    """«Жим лёжа» при своём «Жим штанги лёжа» — похоже, но слово-уточнение
    пропущено: штанга, гантели и Смит — разные упражнения. Предпросмотр не
    ставит цель сам (needs_choice, кандидат первым), импорт без выбора не
    заводит новое молча, а с выбором — кладёт туда и запоминает имя."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    before = await _exercise_count(fresh_db, 111)

    status, unknown = await _preview_entry(client, file_name)
    assert status == {"name": file_name, "status": "new_will_create", "exercise_id": None, "needs_choice": True}
    assert unknown["suggested_exercise_id"] is None
    assert unknown["needs_choice"] is True
    assert unknown["candidates"][0] == {"exercise_id": bench, "name": "Жим штанги лёжа"}

    resp = await client.post("/import/csv", json={"csv": _csv(file_name)})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "exercise_choice_required"
    assert await _exercise_count(fresh_db, 111) == before

    resp = await client.post("/import/csv", json={
        "csv": _csv(file_name), "exercise_mapping": [{"name": file_name, "exercise_id": bench}],
    })
    assert resp.status_code == 200, resp.text
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 2
    assert await _exercise_count(fresh_db, 111) == before

    # Имя закреплено за выбранным упражнением — второй раз не спрашиваем.
    status, unknown = await _preview_entry(client, file_name)
    assert status["exercise_id"] == bench and status["needs_choice"] is False
    assert unknown is None


async def test_commit_with_the_chosen_candidate_does_not_duplicate(fresh_db):
    """Приложение шлёт выбранного кандидата — подходы ложатся в него, второго
    упражнения с той же идентичностью нет."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    _, unknown = await _preview_entry(client, "Жим лёжа")
    resp = await client.post("/import/csv", json={
        "csv": _csv("Жим лёжа"),
        "exercise_mapping": [{"name": "Жим лёжа", "exercise_id": unknown["candidates"][0]["exercise_id"]}],
    })
    assert resp.status_code == 200, resp.text
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 2
    assert await _identity_count(fresh_db, 111, "Жим штанги лёжа") == 1
    assert await fresh_db.find_exercise_by_name(111, "Жим лёжа") is None


@pytest.mark.parametrize("file_name", ["bench press (barbell)"])
async def test_english_athlete_bench_is_found_by_catalog_name(fresh_db, file_name):
    client = await _client(fresh_db, lang="en")
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    assert (await fresh_db.get_exercise(bench))["display_name"] == "Barbell Bench Press"

    status, unknown = await _preview_entry(client, file_name)
    assert status["status"] == "existing" and status["exercise_id"] == bench
    assert unknown["candidates"][0] == {"exercise_id": bench, "name": "Barbell Bench Press"}

    resp = await client.post("/import/csv", json={"csv": _csv(file_name)})
    assert resp.status_code == 200, resp.text
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 2
    assert await _identity_count(fresh_db, 111, "Жим штанги лёжа") == 1


@pytest.mark.parametrize("file_name", ["Bench Press", "Жим лёжа"])
async def test_english_athlete_bench_is_offered_for_shorter_names(fresh_db, file_name):
    client = await _client(fresh_db, lang="en")
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    status, unknown = await _preview_entry(client, file_name)
    assert status["needs_choice"] is True
    assert unknown["candidates"][0] == {"exercise_id": bench, "name": "Barbell Bench Press"}


async def test_russian_athlete_bench_is_found_by_english_file_name(fresh_db):
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    status, _ = await _preview_entry(client, "Bench Press (Barbell)")
    assert status["exercise_id"] == bench


async def test_unknown_name_still_goes_to_new(fresh_db):
    client = await _client(fresh_db)
    await _fork(fresh_db, 111, "Жим штанги лёжа")
    status, unknown = await _preview_entry(client, "Турецкий подъём")
    assert status == {"name": "Турецкий подъём", "status": "new_will_create", "exercise_id": None, "needs_choice": False}
    assert unknown == {"name": "Турецкий подъём", "suggested_exercise_id": None, "needs_choice": False, "candidates": []}

    resp = await client.post("/import/csv", json={"csv": _csv("Турецкий подъём")})
    assert resp.status_code == 200, resp.text
    created = await fresh_db.find_exercise_by_name(111, "Турецкий подъём")
    assert created is not None
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", created["id"]) == 2


async def test_ambiguous_name_offers_candidates_and_preselects_nothing(fresh_db):
    client = await _client(fresh_db)
    barbell = await _fork(fresh_db, 111, "Жим штанги лёжа")
    dumbbell = await _fork(fresh_db, 111, "Жим гантелей лёжа")
    status, unknown = await _preview_entry(client, "Жим лёжа")
    assert status["status"] == "new_will_create"
    assert unknown["suggested_exercise_id"] is None
    assert unknown["needs_choice"] is True
    assert {c["exercise_id"] for c in unknown["candidates"]} == {barbell, dumbbell}

    # Ничего не выбрано — «завести новое» за человека не подставляем: 409, в
    # базе ничего не появилось.
    resp = await client.post("/import/csv", json={"csv": _csv("Жим лёжа")})
    assert resp.status_code == 409, resp.text
    assert await fresh_db.find_exercise_by_name(111, "Жим лёжа") is None
    # Явное «новое» (exercise_id: null) — заводится.
    resp = await client.post("/import/csv", json={
        "csv": _csv("Жим лёжа"), "exercise_mapping": [{"name": "Жим лёжа", "exercise_id": None}],
    })
    assert resp.status_code == 200, resp.text
    assert await fresh_db.find_exercise_by_name(111, "Жим лёжа") is not None
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id IN (?, ?)", barbell, dumbbell) == 0


async def test_one_word_name_is_only_a_candidate(fresh_db):
    """«Жим» слишком широк, чтобы угадывать даже при единственном жиме."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    status, unknown = await _preview_entry(client, "Жим")
    assert status["status"] == "new_will_create"
    assert unknown["suggested_exercise_id"] is None
    assert [c["exercise_id"] for c in unknown["candidates"]] == [bench]


async def test_explicit_new_choice_is_honoured(fresh_db):
    """exercise_id: null — «заведи новое», даже если нашлось похожее; и имя,
    которое клиент с выбором не прислал, тоже остаётся новым."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")
    resp = await client.post("/import/csv", json={
        "csv": _csv("Жим лёжа"), "exercise_mapping": [{"name": "Жим лёжа", "exercise_id": None}],
    })
    assert resp.status_code == 200, resp.text
    assert await fresh_db.find_exercise_by_name(111, "Жим лёжа") is not None
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 0


async def test_model_match_to_an_owned_identity_goes_into_that_exercise(fresh_db, monkeypatch):
    """Имя, которое модель сопоставила с шаблоном, который у атлета уже есть,
    ложится в его упражнение — второго с той же идентичностью не заводится
    (правило H3: та же идентичность — то же упражнение). Предпросмотр
    показывает это так же, как коммит."""
    client = await _client(fresh_db)
    bench = await _fork(fresh_db, 111, "Жим штанги лёжа")

    async def fake_match(user_id, names):
        return {n: "Жим штанги лёжа" for n in names}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)
    status, _ = await _preview_entry(client, "Скамья ПЛ")
    assert status["status"] == "existing" and status["exercise_id"] == bench

    resp = await client.post("/import/csv", json={"csv": _csv("Скамья ПЛ")})
    assert resp.status_code == 200, resp.text
    assert await _identity_count(fresh_db, 111, "Жим штанги лёжа") == 1
    assert await fresh_db.find_exercise_by_name(111, "Скамья ПЛ") is None
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets WHERE exercise_id = ?", bench) == 2


async def test_two_file_names_for_one_new_template_share_one_exercise(fresh_db):
    """«Barbell Bench Press» и «Жим штанги лёжа» у нового атлета — один
    шаблон каталога: заводится одно упражнение, а не два с одной
    идентичностью."""
    client = await _client(fresh_db)
    csv = (
        "date,exercise,weight,reps\n"
        "2024-01-03,Barbell Bench Press,80,8\n"
        "2024-01-05,жим штанги лежа,80,8\n"
    )
    resp = await client.post("/import/csv", json={"csv": csv})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 2
    assert await _identity_count(fresh_db, 111, "Жим штанги лёжа") == 1
