"""REST `/v1` для шаринга программ и упражнений (api_v1_sharing.py).

Тот же приём, что у tests/test_api_v1.py — httpx поверх ASGI, без сокета,
с настоящей проверкой Bearer-токена. Здесь всегда два клиента с разными
telegram_id: владелец создаёт визитку, получатель её открывает и/или
импортирует — ровно то, что в боте делают share_* и open_shared/share_add.
"""

import httpx
import pytest

import api_v1
import config
import db


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id: int):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _program_with_one_day(user_id: int) -> tuple[int, int]:
    """Программа из одного дня с двумя упражнениями — минимальный непустой
    снапшот. Возвращает (program_id, exercise_id второго упражнения)."""
    group_id = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", group_id)
    custom = await db.create_exercise(user_id, "Моя авторская тяга", group_id)
    program_id = await db.create_program(user_id, "PPL")
    routine_id = await db.create_routine(user_id, "День 1", program_id=program_id)
    await db.add_routine_exercise(routine_id, bench, 0, "4x6-8")
    await db.add_routine_exercise(routine_id, custom, 1, None)
    return program_id, custom


# ---------- владелец: создание визитки ----------

@pytest.mark.asyncio
async def test_share_program_returns_token(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id, _ = await _program_with_one_day(111)

    resp = await owner.post(f"/share/programs/{program_id}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "program"
    assert body["token"]
    assert body["start_param"] == f"sh_{body['token']}"


@pytest.mark.asyncio
async def test_share_someone_elses_program_is_404(fresh_db, client_factory):
    program_id, _ = await _program_with_one_day(111)
    other = await _linked_client(fresh_db, client_factory, 222)

    resp = await other.post(f"/share/programs/{program_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_share_empty_program_is_rejected(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id = await db.create_program(111, "Empty")

    resp = await owner.post(f"/share/programs/{program_id}")
    assert resp.status_code == 400
    assert resp.json()["error"] == "empty_program"


@pytest.mark.asyncio
async def test_share_routine_returns_token(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    group_id = await db.create_muscle_group(111, "Грудь")
    bench = await db.create_exercise(111, "Жим лёжа", group_id)
    routine_id = await db.create_routine(111, "День 1")
    await db.add_routine_exercise(routine_id, bench, 0, "4x6-8")

    resp = await owner.post(f"/share/routines/{routine_id}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "routine"
    assert body["token"]


@pytest.mark.asyncio
async def test_share_empty_routine_is_rejected(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    routine_id = await db.create_routine(111, "Пустой день")

    resp = await owner.post(f"/share/routines/{routine_id}")
    assert resp.status_code == 400
    assert resp.json()["error"] == "empty_routine"


@pytest.mark.asyncio
async def test_share_someone_elses_routine_is_404(fresh_db, client_factory):
    routine_id = await db.create_routine(111, "День 1")
    other = await _linked_client(fresh_db, client_factory, 222)

    resp = await other.post(f"/share/routines/{routine_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_recipient_imports_routine_as_standalone_day(fresh_db, client_factory):
    """Тело того же снапшота, что и program["days"][i] — import_share уже умеет
    kind="routine" (см. handlers.sharing.import_routine), новая ручка только
    выпускает такой token со стороны владельца."""
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    group_id = await db.create_muscle_group(111, "Грудь")
    bench = await db.create_exercise(111, "Жим лёжа", group_id)
    routine_id = await db.create_routine(111, "День 1")
    await db.add_routine_exercise(routine_id, bench, 0, "4x6-8")
    token = (await owner.post(f"/share/routines/{routine_id}")).json()["token"]

    preview = await recipient.get(f"/share/{token}")
    assert preview.status_code == 200
    assert preview.json()["kind"] == "routine"
    assert preview.json()["routine"]["name"] == "День 1"

    imported = await recipient.post(f"/share/{token}/import")
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert body["kind"] == "routine"
    assert body["name"] == "День 1"

    listed = (await recipient.get("/routines")).json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["routine_id"]


@pytest.mark.asyncio
async def test_share_exercise_returns_token(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    group_id = await db.create_muscle_group(111, "Спина")
    ex_id = await db.create_exercise(111, "Тяга штанги", group_id)

    resp = await owner.post(f"/share/exercises/{ex_id}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "exercise"
    assert body["token"]


@pytest.mark.asyncio
async def test_share_someone_elses_exercise_is_404(fresh_db, client_factory):
    group_id = await db.create_muscle_group(111, "Спина")
    ex_id = await db.create_exercise(111, "Тяга штанги", group_id)
    other = await _linked_client(fresh_db, client_factory, 222)

    resp = await other.post(f"/share/exercises/{ex_id}")
    assert resp.status_code == 404


# ---------- получатель: превью без импорта ----------

@pytest.mark.asyncio
async def test_recipient_sees_preview_without_importing(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await recipient.get(f"/share/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "program"
    assert body["is_own"] is False
    assert body["owner"]["username"] == "user111"
    assert body["program"]["name"] == "PPL"
    assert [d["name"] for d in body["program"]["days"]] == ["День 1"]
    assert len(body["program"]["days"][0]["exercises"]) == 2

    # Превью не импортирует ничего — у получателя как не было программ, так и нет.
    listed = await recipient.get("/programs")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_preview_marks_owner_as_own(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await owner.get(f"/share/{token}")
    assert resp.status_code == 200
    assert resp.json()["is_own"] is True


@pytest.mark.asyncio
async def test_preview_survives_rename_and_delete_of_original(fresh_db, client_factory):
    """Снапшот, а не живая ссылка: переименование/удаление оригинала не портит
    превью — в этом весь смысл шаринга снапшотом."""
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    renamed = await owner.patch(f"/programs/{program_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    deleted = await owner.delete(f"/programs/{program_id}")
    assert deleted.status_code == 200

    resp = await recipient.get(f"/share/{token}")
    assert resp.status_code == 200
    assert resp.json()["program"]["name"] == "PPL"


@pytest.mark.asyncio
async def test_broken_token_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    resp = await client.get("/share/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/share/whatever")
    assert resp.status_code == 401


# ---------- получатель: импорт ----------

@pytest.mark.asyncio
async def test_recipient_imports_program_and_gets_own_copy(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    imported = await recipient.post(f"/share/{token}/import")
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert body["kind"] == "program"
    assert body["name"] == "PPL"
    assert body["days"] == 1

    listed = (await recipient.get("/programs")).json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["program_id"]
    days = (await recipient.get(f"/programs/{body['program_id']}")).json()["days"]
    assert len(days) == 1
    exercises = (await recipient.get(f"/routines/{days[0]['id']}")).json()["exercises"]
    assert len(exercises) == 2

    # Оригинал владельца не тронут импортом.
    owner_programs = (await owner.get("/programs")).json()
    assert len(owner_programs) == 1
    assert owner_programs[0]["id"] == program_id


@pytest.mark.asyncio
async def test_import_creates_unknown_exercise_under_fallback_group(fresh_db, client_factory):
    """Резолв по имени: своё совпадение есть только у "Жим лёжа" (общее имя
    из каталога), "Моя авторская тяга" получатель заводит у себя впервые —
    под "Другое" (см. handlers.sharing._resolve_exercise)."""
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await recipient.post(f"/share/{token}/import")
    assert resp.status_code == 201
    exercise = await db.find_exercise_by_name(222, "Моя авторская тяга")
    assert exercise is not None


@pytest.mark.asyncio
async def test_recipient_imports_exercise(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    group_id = await db.create_muscle_group(111, "Спина")
    ex_id = await db.create_exercise(111, "Тяга штанги", group_id)
    token = (await owner.post(f"/share/exercises/{ex_id}")).json()["token"]

    resp = await recipient.post(f"/share/{token}/import")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "exercise"
    assert body["name"] == "Тяга штанги"

    fetched = await db.get_exercise(body["exercise_id"])
    assert fetched is not None
    assert fetched["display_name"] == "Тяга штанги"
    assert fetched["user_id"] == 222


@pytest.mark.asyncio
async def test_import_exercise_conflict_when_name_taken(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    group_id = await db.create_muscle_group(111, "Спина")
    ex_id = await db.create_exercise(111, "Тяга штанги", group_id)
    token = (await owner.post(f"/share/exercises/{ex_id}")).json()["token"]

    # У получателя уже есть своё упражнение с этим именем.
    recipient_group = await db.create_muscle_group(222, "Спина")
    await db.create_exercise(222, "Тяга штанги", recipient_group)

    resp = await recipient.post(f"/share/{token}/import")
    assert resp.status_code == 409
    assert resp.json()["error"] == "exercise_exists"


@pytest.mark.asyncio
async def test_owner_cannot_import_own_card(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await owner.post(f"/share/{token}/import")
    assert resp.status_code == 400
    assert resp.json()["error"] == "own_card"


@pytest.mark.asyncio
async def test_import_is_not_triggered_by_get(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    for _ in range(3):
        await recipient.get(f"/share/{token}")

    assert (await recipient.get("/programs")).json() == []


@pytest.mark.asyncio
async def test_import_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/share/whatever/import")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_import_broken_token_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    resp = await client.post("/share/does-not-exist/import")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_import_respects_routine_budget(fresh_db, client_factory, monkeypatch):
    """Тот же общий бюджет, что у бота (db.routine_budget) — присланная
    программа не должна пробивать MAX_ROUTINES_PER_USER в обход бота."""
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    monkeypatch.setattr(config, "MAX_ROUTINES_PER_USER", 0)
    resp = await recipient.post(f"/share/{token}/import")
    assert resp.status_code == 403
    assert resp.json()["error"] == "routine_limit_reached"
    assert (await recipient.get("/programs")).json() == []


@pytest.mark.asyncio
async def test_two_concurrent_import_requests_do_not_blow_past_the_routine_budget(
    fresh_db, client_factory, monkeypatch
):
    """Проверка бюджета (`db.routine_budget`) и сама запись импортированной
    программы не атомарны — два запроса `/share/{token}/import` почти
    одновременно (двойной тап в приложении, повтор после таймаута) читают
    один и тот же бюджет до того, как первый успел закоммитить, и вместе
    заводят у получателя больше программ, чем разрешает
    `MAX_ROUTINES_PER_USER`."""
    import asyncio

    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token1 = (await owner.post(f"/share/programs/{program_id}")).json()["token"]
    token2 = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    await db.create_routine(222, "Existing1")
    await db.create_routine(222, "Existing2")
    monkeypatch.setattr(config, "MAX_ROUTINES_PER_USER", 3)

    responses = await asyncio.gather(
        recipient.post(f"/share/{token1}/import"),
        recipient.post(f"/share/{token2}/import"),
    )
    statuses = sorted(r.status_code for r in responses)
    # Один импорт проходит (201), второй либо честно отказывает по бюджету
    # (403), либо натыкается на конкурентный импорт того же аккаунта (409) —
    # но не проходят оба сразу.
    assert statuses[0] == 201
    assert statuses[1] in (403, 409)
    assert await db.count_routines(222) <= 3


# ---------- владелец: отзыв визитки ----------

@pytest.mark.asyncio
async def test_owner_revokes_own_card(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await owner.delete(f"/share/{token}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revoked": True, "kind": "program", "taken_count": 0}


@pytest.mark.asyncio
async def test_revoking_someone_elses_card_is_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    other = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]

    resp = await other.delete(f"/share/{token}")
    assert resp.status_code == 404
    # Отказ не должен быть половинчатым: чужая ссылка после 404 обязана
    # остаться рабочей, иначе «отзыв» становился бы оружием против автора.
    assert (await other.get(f"/share/{token}")).status_code == 200


@pytest.mark.asyncio
async def test_revoked_card_stops_opening(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]
    assert (await recipient.get(f"/share/{token}")).status_code == 200

    await owner.delete(f"/share/{token}")

    assert (await recipient.get(f"/share/{token}")).status_code == 404
    assert (await recipient.post(f"/share/{token}/import")).status_code == 404


@pytest.mark.asyncio
async def test_revoke_reports_how_many_already_took_it(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    recipient = await _linked_client(fresh_db, client_factory, 222)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]
    assert (await recipient.post(f"/share/{token}/import")).status_code == 201

    resp = await owner.delete(f"/share/{token}")
    assert resp.json()["taken_count"] == 1


@pytest.mark.asyncio
async def test_second_revoke_is_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, 111)
    program_id, _ = await _program_with_one_day(111)
    token = (await owner.post(f"/share/programs/{program_id}")).json()["token"]
    assert (await owner.delete(f"/share/{token}")).status_code == 200

    resp = await owner.delete(f"/share/{token}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_revoke_broken_token_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    resp = await client.delete("/share/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revoke_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.delete("/share/whatever")
    assert resp.status_code == 401
