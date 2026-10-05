"""Офлайн-синхронизация: старт тренировки с `client_id`/`started_at` и конец с
`finished_at`. Подходы идемпотентны давно (`idempotency_key`, см.
test_api_v1_idempotent_sets) — здесь проверяется только то, что добавилось."""

import datetime as dt
import uuid

import httpx
import pytest

import api_v1


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"t{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _ago(**kw) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _count(fresh_db, sql, *args):
    cur = await fresh_db.conn().execute(sql, args)
    return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_same_client_id_returns_same_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    first = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(hours=3)})
    assert first.status_code == 201
    second = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(hours=3)})
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["client_id"] == cid
    assert await _count(fresh_db, "SELECT COUNT(*) FROM workouts") == 1


@pytest.mark.asyncio
async def test_client_id_is_normalized(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    first = await client.post("/workouts/active", json={"client_id": cid.upper()})
    second = await client.post("/workouts/active", json={"client_id": cid})
    assert first.json()["client_id"] == cid
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_retry_after_finish_returns_finished_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    wid = (await client.post("/workouts/active", json={"client_id": cid})).json()["id"]
    ex = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 100, "reps": 5})
    assert (await client.post(f"/workouts/{wid}/finish", json={})).status_code == 200
    again = await client.post("/workouts/active", json={"client_id": cid})
    assert again.status_code == 200
    assert again.json()["id"] == wid
    assert again.json()["status"] == "finished"
    assert await _count(fresh_db, "SELECT COUNT(*) FROM workouts") == 1


@pytest.mark.asyncio
async def test_client_id_is_per_user(fresh_db, client_factory):
    a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    cid = str(uuid.uuid4())
    wa = (await a.post("/workouts/active", json={"client_id": cid})).json()["id"]
    resp = await b.post("/workouts/active", json={"client_id": cid})
    assert resp.status_code == 201
    assert resp.json()["id"] != wa


@pytest.mark.asyncio
async def test_existing_active_workout_adopts_client_id(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid = (await client.post("/workouts/active")).json()["id"]
    cid = str(uuid.uuid4())
    resp = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(hours=2)})
    assert resp.status_code == 200
    assert resp.json()["id"] == wid
    assert resp.json()["client_id"] == cid
    other = await client.post("/workouts/active", json={"client_id": str(uuid.uuid4())})
    assert other.json()["id"] == wid
    assert other.json()["client_id"] == cid, "чужую метку перезаписывать нельзя"
    assert await _count(fresh_db, "SELECT COUNT(*) FROM workouts") == 1


@pytest.mark.asyncio
async def test_old_clients_without_client_id_unchanged(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    first = await client.post("/workouts/active")
    assert first.status_code == 201
    assert first.json()["client_id"] is None
    assert (await client.post("/workouts/active")).json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_past_started_at_is_kept(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    resp = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(days=2)})
    assert resp.status_code == 201
    want = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(tzinfo=None)
    got = dt.datetime.fromisoformat(resp.json()["started_at"])
    assert abs((got - want).total_seconds()) < 5


@pytest.mark.asyncio
async def test_started_at_with_offset_is_converted_to_utc(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    local = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))) - dt.timedelta(hours=1)
    resp = await client.post(
        "/workouts/active",
        json={"client_id": str(uuid.uuid4()), "started_at": local.isoformat(timespec="seconds")},
    )
    assert resp.status_code == 201
    want = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(tzinfo=None)
    got = dt.datetime.fromisoformat(resp.json()["started_at"])
    assert abs((got - want).total_seconds()) < 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_extra, key",
    [
        ({"client_id": "not-a-uuid"}, "input.client_id_invalid"),
        ({"client_id": 5}, None),
        ({"started_at": "2026-01-01T10:00:00Z"}, "input.client_id_required"),
        ({"client_id": "@", "started_at": "x"}, "input.client_id_invalid"),
    ],
)
async def test_validation_errors_are_400(fresh_db, client_factory, body_extra, key):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/workouts/active", json=body_extra)
    assert resp.status_code == 400, resp.text
    assert await _count(fresh_db, "SELECT COUNT(*) FROM workouts") == 0


@pytest.mark.asyncio
async def test_started_at_bounds(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    for value in (_ago(minutes=-30), _ago(days=8), "вчера", "2026-13-40T99:00:00Z"):
        resp = await client.post("/workouts/active", json={"client_id": cid, "started_at": value})
        assert resp.status_code == 400, (value, resp.text)
    # Небольшой уход часов телефона вперёд — допустим и прижимается к «сейчас».
    resp = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(minutes=-2)})
    assert resp.status_code == 201
    assert dt.datetime.fromisoformat(resp.json()["started_at"]) <= dt.datetime.now()


@pytest.mark.asyncio
async def test_error_text_is_localized(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.set_user_lang(111, "en")
    resp = await client.post(
        "/workouts/active", json={"client_id": str(uuid.uuid4()), "started_at": _ago(days=30)}
    )
    assert resp.status_code == 400
    assert "week" in resp.json()["message"]
    assert not any("а" <= ch.lower() <= "я" for ch in resp.json()["message"])


@pytest.mark.asyncio
async def test_offline_sets_retry_and_finish_with_client_times(fresh_db, client_factory):
    """Весь офлайн-путь: старт в прошлом, подходы с ключами (повтор не
    дублирует), finish с реальным концом. Рекорд и значки — тем же кодом, что у
    живой тренировки."""
    client = await _linked_client(fresh_db, client_factory)
    ex = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    cid = str(uuid.uuid4())
    started = _ago(hours=26)
    wid = (await client.post("/workouts/active", json={"client_id": cid, "started_at": started})).json()["id"]
    for _ in range(2):  # повтор после «потерянного ответа»
        await client.post(f"/workouts/{wid}/sets", json={
            "exercise_id": ex, "weight": 80, "reps": 8, "idempotency_key": "s1"})
    ended = _ago(hours=25)
    fin = await client.post(f"/workouts/{wid}/finish", json={"finished_at": ended})
    assert fin.status_code == 200, fin.text
    body = fin.json()
    assert body["status"] == "finished"
    assert body["finished_at"].startswith(ended[:13])
    assert len(body["blocks"][0]["exercises"][0]["sets"]) == 1
    assert "rewards" in body
    # Повтор синхронизации целиком: тот же id, ничего нового.
    again = await client.post("/workouts/active", json={"client_id": cid, "started_at": started})
    assert again.json()["id"] == wid
    assert await _count(fresh_db, "SELECT COUNT(*) FROM workouts") == 1
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets") == 1


@pytest.mark.asyncio
async def test_finish_rejects_bad_finished_at(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    ex = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    wid = (await client.post(
        "/workouts/active", json={"client_id": str(uuid.uuid4()), "started_at": _ago(hours=3)}
    )).json()["id"]
    await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 80, "reps": 8})
    before = await client.post(f"/workouts/{wid}/finish", json={"finished_at": _ago(hours=5)})
    assert before.status_code == 400
    assert (await client.get(f"/workouts/{wid}")).json()["status"] == "active"


@pytest.mark.asyncio
async def test_finish_clamps_future_finished_at_to_server_now(fresh_db, client_factory):
    """Часы телефона убежали вперёд: финиш не теряется 400-й, а ставится на «сейчас»
    сервера (и не раньше начала)."""
    client = await _linked_client(fresh_db, client_factory)
    ex = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    started = _ago(hours=3)
    wid = (await client.post(
        "/workouts/active", json={"client_id": str(uuid.uuid4()), "started_at": started}
    )).json()["id"]
    await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 80, "reps": 8})
    t0 = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    fin = await client.post(f"/workouts/{wid}/finish", json={"finished_at": _ago(hours=-2)})
    assert fin.status_code == 200, fin.text
    body = fin.json()
    assert body["status"] == "finished"
    finished = dt.datetime.fromisoformat(body["finished_at"])
    assert t0 <= finished <= t0 + dt.timedelta(minutes=1)
    assert finished >= dt.datetime.fromisoformat(body["started_at"])


@pytest.mark.asyncio
async def test_migration_adds_column_and_unique_index(fresh_db):
    cur = await fresh_db.conn().execute("PRAGMA index_list(workouts)")
    names = {r["name"]: r["unique"] for r in await cur.fetchall()}
    assert names.get("idx_workouts_user_client") == 1


# ---------- adopted: чужая шедшая тренировка ----------


@pytest.mark.asyncio
async def test_start_reports_adopted_for_existing_active_workout(fresh_db, client_factory):
    """Тренировка идёт (начата в боте): офлайн-старт по client_id усыновляет её, и
    приложению об этом говорят — иначе его досылка закроет её за человека."""
    client = await _linked_client(fresh_db, client_factory)
    wid = (await client.post("/workouts/active")).json()["id"]
    cid = str(uuid.uuid4())
    first = await client.post("/workouts/active", json={"client_id": cid, "started_at": _ago(hours=2)})
    assert first.status_code == 200
    assert first.json()["id"] == wid
    assert first.json()["adopted"] is True
    # Повтор после потерянного ответа находится уже по метке — признак не теряется.
    again = await client.post("/workouts/active", json={"client_id": cid})
    assert again.json()["id"] == wid
    assert again.json()["adopted"] is True
    # Метка на чужой шедшей тренировке с другой меткой — тоже «чужая».
    other = await client.post("/workouts/active", json={"client_id": str(uuid.uuid4())})
    assert other.json()["id"] == wid
    assert other.json()["adopted"] is True


@pytest.mark.asyncio
async def test_start_reports_not_adopted_for_own_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    cid = str(uuid.uuid4())
    first = await client.post("/workouts/active", json={"client_id": cid})
    assert first.status_code == 201
    assert first.json()["adopted"] is False
    again = await client.post("/workouts/active", json={"client_id": cid})
    assert again.json()["adopted"] is False


@pytest.mark.asyncio
async def test_adopted_absent_without_client_id(fresh_db, client_factory):
    """Старый клиент и обычный старт признака не получают: ответ прежний."""
    client = await _linked_client(fresh_db, client_factory)
    assert "adopted" not in (await client.post("/workouts/active")).json()


@pytest.mark.asyncio
async def test_adopted_workout_still_takes_sets(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    ex = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    wid = (await client.post("/workouts/active")).json()["id"]
    adopted = await client.post("/workouts/active", json={"client_id": str(uuid.uuid4())})
    assert adopted.json()["adopted"] is True
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "idempotency_key": "off-1"})
    assert resp.status_code == 201
    assert (await client.get(f"/workouts/{wid}")).json()["status"] == "active"


@pytest.mark.asyncio
async def test_migration_adds_client_adopted_column(fresh_db):
    cur = await fresh_db.conn().execute("PRAGMA table_info(workouts)")
    assert "client_adopted" in {r["name"] for r in await cur.fetchall()}


# ---------- created_at подхода ----------


async def _set_created_at(fresh_db, set_id):
    cur = await fresh_db.conn().execute("SELECT created_at FROM sets WHERE id = ?", (set_id,))
    return dt.datetime.fromisoformat((await cur.fetchone())["created_at"])


def _moment(**kw) -> dt.datetime:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).replace(tzinfo=None, microsecond=0)


async def _offline_workout(client, hours=3):
    ex = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    wid = (await client.post(
        "/workouts/active", json={"client_id": str(uuid.uuid4()), "started_at": _ago(hours=hours)}
    )).json()["id"]
    return wid, ex


@pytest.mark.asyncio
async def test_set_created_at_within_workout_is_kept(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    want = _moment(hours=1)
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": want.isoformat() + "Z"})
    assert resp.status_code == 201, resp.text
    assert await _set_created_at(fresh_db, resp.json()["id"]) == want
    assert resp.json()["created_at"].startswith(want.isoformat()[:16])


@pytest.mark.asyncio
async def test_set_created_at_offset_is_converted_to_utc(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    want = _moment(hours=1)
    local = (want + dt.timedelta(hours=3)).isoformat() + "+03:00"
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": local})
    assert await _set_created_at(fresh_db, resp.json()["id"]) == want


@pytest.mark.asyncio
async def test_set_created_at_in_future_is_clamped_to_now(fresh_db, client_factory):
    """Часы телефона убежали вперёд: подход не теряется 400-й, встаёт на «сейчас»."""
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    t0 = _moment()
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": _ago(hours=-5)})
    assert resp.status_code == 201, resp.text
    got = await _set_created_at(fresh_db, resp.json()["id"])
    assert t0 <= got <= t0 + dt.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_set_created_at_before_start_is_pulled_to_start(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client, hours=3)
    started = dt.datetime.fromisoformat((await client.get(f"/workouts/{wid}")).json()["started_at"])
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": _ago(hours=6)})
    assert resp.status_code == 201, resp.text
    assert await _set_created_at(fresh_db, resp.json()["id"]) == started


@pytest.mark.asyncio
async def test_set_created_at_too_old_is_ignored_not_rejected(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    t0 = _moment()
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": _ago(days=30)})
    assert resp.status_code == 201, resp.text
    assert await _set_created_at(fresh_db, resp.json()["id"]) >= t0


@pytest.mark.asyncio
async def test_set_created_at_garbage_is_400_and_nothing_written(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    resp = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "created_at": "вчера"})
    assert resp.status_code == 400
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets") == 0


@pytest.mark.asyncio
async def test_set_without_created_at_is_now(fresh_db, client_factory):
    """Старый клиент поля не шлёт — поведение прежнее."""
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    t0 = _moment()
    resp = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 80, "reps": 8})
    got = await _set_created_at(fresh_db, resp.json()["id"])
    assert t0 <= got <= t0 + dt.timedelta(minutes=1)


@pytest.mark.asyncio
async def test_parse_sets_take_created_at(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    want = _moment(hours=1)
    resp = await client.post(f"/workouts/{wid}/sets/parse", json={
        "exercise_id": ex, "text": "100 8, 100 7", "created_at": want.isoformat() + "Z"})
    assert resp.status_code == 201, resp.text
    sets = resp.json()["sets"]
    assert len(sets) == 2
    for item in sets:
        assert await _set_created_at(fresh_db, item["id"]) == want


# ---------- повтор подхода в закрытую тренировку ----------


@pytest.mark.asyncio
async def test_retry_of_recorded_set_into_finished_workout_is_replayed(fresh_db, client_factory):
    """Подход записан, ответ потерялся, тренировку закончили — повтор с тем же
    ключом получает записанный подход, а не 409: приложение не должно считать
    лежащий в истории подход потерянным."""
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    body = {"exercise_id": ex, "weight": 80, "reps": 8, "idempotency_key": "k-1"}
    first = await client.post(f"/workouts/{wid}/sets", json=body)
    assert first.status_code == 201
    assert (await client.post(f"/workouts/{wid}/finish", json={})).status_code == 200
    retry = await client.post(f"/workouts/{wid}/sets", json=body)
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets") == 1


@pytest.mark.asyncio
async def test_retry_of_recorded_line_into_finished_workout_is_replayed(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    body = {"exercise_id": ex, "text": "100 8, 100 7", "idempotency_key": "line-1"}
    first = await client.post(f"/workouts/{wid}/sets/parse", json=body)
    await client.post(f"/workouts/{wid}/finish", json={})
    retry = await client.post(f"/workouts/{wid}/sets/parse", json=body)
    assert retry.status_code == 201
    assert [s["id"] for s in retry.json()["sets"]] == [s["id"] for s in first.json()["sets"]]


@pytest.mark.asyncio
async def test_new_set_into_finished_workout_is_still_409(fresh_db, client_factory):
    """Повтор не открывает закрытую тренировку: новый ключ, чужой ключ и подход без
    ключа — по-прежнему 409."""
    client = await _linked_client(fresh_db, client_factory)
    wid, ex = await _offline_workout(client)
    await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 80, "reps": 8, "idempotency_key": "k-1"})
    await client.post(f"/workouts/{wid}/finish", json={})
    for extra in ({"idempotency_key": "k-2"}, {}):
        late = await client.post(f"/workouts/{wid}/sets", json={
            "exercise_id": ex, "weight": 80, "reps": 8, **extra})
        assert late.status_code == 409
        assert late.json()["error"] == "workout_finished"
    # Тот же ключ, но к другой (закрытой) тренировке — не повтор.
    other_wid = (await client.post("/workouts/active", json={"client_id": str(uuid.uuid4())})).json()["id"]
    await client.post(f"/workouts/{other_wid}/sets", json={
        "exercise_id": ex, "weight": 70, "reps": 8, "idempotency_key": "k-9"})
    await client.post(f"/workouts/{other_wid}/finish", json={})
    cross = await client.post(f"/workouts/{wid}/sets", json={
        "exercise_id": ex, "weight": 70, "reps": 8, "idempotency_key": "k-9"})
    assert cross.status_code == 409
    assert await _count(fresh_db, "SELECT COUNT(*) FROM sets") == 2
