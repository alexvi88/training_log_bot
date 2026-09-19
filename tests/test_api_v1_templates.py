"""REST `/v1` для каталога упражнений (api_v1_templates.py).

Тот же приём, что у соседних доменов: httpx поверх ASGI без сокета. Каталог
шаблонов (`is_template=1`) сеется вместе с базой (см. tests/conftest.py) —
искать реальный шаблон, а не заводить фикстуру ещё раз.
"""

import httpx
import pytest

import api_v1
import db


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _bench_press_template(fresh_db) -> dict:
    matches = await fresh_db.search_exercise_templates(999999999, "жим штанги лёжа")
    assert matches, "seed_data должен нести «Жим штанги лёжа» в каталоге"
    return dict(matches[0])


# ---------- поиск и просмотр ----------

@pytest.mark.asyncio
async def test_search_finds_catalog_template(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates", params={"query": "жим штанги лёжа"})
    assert resp.status_code == 200, resp.text
    names = [t["name"] for t in resp.json()]
    assert "Жим штанги лёжа" in names


@pytest.mark.asyncio
async def test_search_skips_template_user_already_owns(fresh_db, client_factory):
    """Та же дедупликация по identity, что и в db.search_exercise_templates —
    здесь только транспорт, не вторая копия правила."""
    client = await _linked_client(fresh_db, client_factory)
    group_id = await db.create_muscle_group(111, "Грудь")
    await db.create_exercise(111, "Жим штанги лёжа", group_id)

    resp = await client.get("/exercise-templates", params={"query": "жим штанги лёжа"})
    assert resp.status_code == 200
    assert "Жим штанги лёжа" not in [t["name"] for t in resp.json()]


@pytest.mark.asyncio
async def test_search_localizes_name_for_english_user(fresh_db, client_factory):
    await db.get_or_create_user(telegram_id=333, username="en_user", language_code="en")
    code = await db.issue_oauth_link_code(333, ttl_seconds=600, digits=8)
    client = client_factory()
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get("/exercise-templates", params={"query": "bench press"})
    assert resp.status_code == 200, resp.text
    names = [t["name"] for t in resp.json()]
    assert any("Bench" in n for n in names)


@pytest.mark.asyncio
async def test_browse_by_group_lists_templates(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _bench_press_template(fresh_db)

    resp = await client.get("/exercise-templates", params={"group_id": template["primary_group_id"]})
    assert resp.status_code == 200, resp.text
    ids = [t["id"] for t in resp.json()]
    assert template["id"] in ids


@pytest.mark.asyncio
async def test_search_requires_query_or_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates")
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_get_template_detail_has_media_and_description(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _bench_press_template(fresh_db)

    resp = await client.get(f"/exercise-templates/{template['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == template["id"]
    assert body["name"] == "Жим штанги лёжа"
    assert "media" in body and "images" in body["media"]
    assert "description" in body


@pytest.mark.asyncio
async def test_get_template_unknown_id_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercise-templates/999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_template_rejects_owned_exercise_id(fresh_db, client_factory):
    """id из чужого/своего каталога упражнений (is_template=0) — не шаблон,
    даже если id существует."""
    client = await _linked_client(fresh_db, client_factory)
    group_id = await db.create_muscle_group(111, "Грудь")
    ex_id = await db.create_exercise(111, "Моё упражнение", group_id)

    resp = await client.get(f"/exercise-templates/{ex_id}")
    assert resp.status_code == 404


# ---------- форк в свою копию ----------

@pytest.mark.asyncio
async def test_add_template_forks_into_own_exercise(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _bench_press_template(fresh_db)

    resp = await client.post(f"/exercise-templates/{template['id']}/add")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == "Жим штанги лёжа"
    assert body["original_name"] == "Жим штанги лёжа"
    assert body["is_archived"] is False

    listed = await client.get("/exercises")
    assert any(e["id"] == body["id"] for e in listed.json())


@pytest.mark.asyncio
async def test_add_template_twice_returns_the_same_fork(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _bench_press_template(fresh_db)

    first = (await client.post(f"/exercise-templates/{template['id']}/add")).json()
    second = (await client.post(f"/exercise-templates/{template['id']}/add")).json()
    assert first["id"] == second["id"]


@pytest.mark.asyncio
async def test_add_unknown_template_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/exercise-templates/999999/add")
    assert resp.status_code == 404


# ---------- 401 без токена ----------

@pytest.mark.asyncio
async def test_requires_auth(fresh_db, client_factory):
    client = client_factory()
    assert (await client.get("/exercise-templates", params={"query": "жим"})).status_code == 401
    assert (await client.get("/exercise-templates/1")).status_code == 401
    assert (await client.post("/exercise-templates/1/add")).status_code == 401
