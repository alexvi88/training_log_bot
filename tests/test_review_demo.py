"""Демо-аккаунт App Review: POST /v1/auth/password и заполнение историей
(review_demo.py). Apple отклонил сборку по Guideline 2.1(a) — ревьюеру нужен
логин и пароль от аккаунта, где уже есть тренировки, рекорды и значки."""

import datetime as dt

import httpx
import pytest

import api_v1
import config
import review_demo

USERNAME = "appreview"
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def demo_config(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", USERNAME)
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", PASSWORD)
    review_demo.reset_failures()
    yield
    review_demo.reset_failures()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _login(client, **extra):
    return await client.post("/auth/password", json={"username": USERNAME, "password": PASSWORD, **extra})


@pytest.mark.asyncio
async def test_login_returns_token_in_link_response_shape_and_token_works(fresh_db):
    client = _client()
    resp = await _login(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Та же форма, что у /auth/apple — LinkResponse в iOS декодирует её как есть.
    assert set(body) == {"token", "user_id", "unit", "lang"}
    assert body["user_id"] < 0  # app-only, синтетический id
    assert body["lang"] == "en"
    assert body["unit"] == "kg"

    client.headers["Authorization"] = f"Bearer {body['token']}"
    me = await client.get("/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == body["user_id"]

    workouts = await client.get("/workouts")
    assert workouts.status_code == 200


@pytest.mark.asyncio
async def test_login_respects_lang_for_new_account(fresh_db):
    resp = await _login(_client(), lang="ru")
    assert resp.status_code == 200
    assert resp.json()["lang"] == "ru"


@pytest.mark.asyncio
async def test_wrong_password_is_401_with_error_envelope(fresh_db):
    client = _client()
    resp = await client.post(
        "/auth/password", json={"username": USERNAME, "password": "nope"},
        headers={"Accept-Language": "en-US"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "invalid_credentials"
    assert body["message"] and body["detail"]
    # и неверный логин с верным паролем — тоже 401
    resp = await client.post("/auth/password", json={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 401
    # ни одного аккаунта не завели
    assert await fresh_db.resolve_auth_identity(review_demo.PROVIDER, USERNAME) is None


@pytest.mark.asyncio
async def test_missing_fields_are_400(fresh_db):
    resp = await _client().post("/auth/password", json={"username": USERNAME})
    assert resp.status_code == 400


@pytest.mark.parametrize("unset", ["REVIEW_DEMO_USERNAME", "REVIEW_DEMO_PASSWORD"])
@pytest.mark.asyncio
async def test_unset_config_is_404_like_missing_route(fresh_db, monkeypatch, unset):
    monkeypatch.setattr(config, unset, "")
    client = _client()
    resp = await client.post("/auth/password", json={"username": "", "password": ""})
    missing = await client.post("/auth/does-not-exist", json={})
    assert resp.status_code == 404
    assert (resp.status_code, resp.text) == (missing.status_code, missing.text)


@pytest.mark.asyncio
async def test_rate_limit_after_repeated_failures(fresh_db):
    client = _client()
    headers = {"Fly-Client-IP": "203.0.113.7"}
    for _ in range(review_demo.FAILURE_LIMIT_PER_IP):
        resp = await client.post(
            "/auth/password", json={"username": USERNAME, "password": "bad"}, headers=headers
        )
        assert resp.status_code == 401
    # после лимита запирается даже верный пароль — иначе лимит не мешал бы перебору
    resp = await client.post(
        "/auth/password", json={"username": USERNAME, "password": PASSWORD}, headers=headers
    )
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    # с другого адреса входит как обычно
    other = await client.post(
        "/auth/password", json={"username": USERNAME, "password": PASSWORD},
        headers={"Fly-Client-IP": "198.51.100.1"},
    )
    assert other.status_code == 200


@pytest.mark.asyncio
async def test_second_login_same_user_new_token_no_reseed(fresh_db):
    client = _client()
    first = (await _login(client)).json()
    count = await fresh_db.count_workouts(first["user_id"])
    second = (await _login(client, lang="ru")).json()
    assert second["user_id"] == first["user_id"]
    assert second["token"] != first["token"]
    # язык существующего аккаунта не переписывается
    assert second["lang"] == "en"
    assert await fresh_db.count_workouts(first["user_id"]) == count


@pytest.mark.asyncio
async def test_seeding_creates_history_records_and_is_idempotent(fresh_db):
    client = _client()
    body = (await _login(client)).json()
    user_id = body["user_id"]
    client.headers["Authorization"] = f"Bearer {body['token']}"

    assert await fresh_db.count_workouts(user_id) == review_demo.TOTAL_WORKOUTS
    assert await fresh_db.count_workouts(user_id, "active") == 0
    assert len(await fresh_db.list_bodyweight_logs(user_id)) >= 4

    # Всё в прошлом, свежее — не старше недели, самое старое — в пределах ~5 недель.
    workouts = (await client.get("/workouts")).json()
    items = workouts["workouts"] if isinstance(workouts, dict) else workouts
    assert len(items) == review_demo.TOTAL_WORKOUTS
    now = dt.datetime.now()
    starts = [dt.datetime.fromisoformat(w["started_at"]) for w in items]
    finishes = [dt.datetime.fromisoformat(w["finished_at"]) for w in items]
    assert all(s < f < now for s, f in zip(starts, finishes))
    assert now - max(starts) < dt.timedelta(days=7)
    assert now - min(starts) < dt.timedelta(days=36)

    # Подходы лежат внутри своей тренировки — иначе длительность пустая.
    for w in items:
        detail = (await client.get(f"/workouts/{w['id']}")).json()
        stamps = []

        def walk(node):
            if isinstance(node, dict):
                if "reps" in node and "created_at" in node:
                    stamps.append(dt.datetime.fromisoformat(node["created_at"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(detail)
        assert stamps, detail
        start = dt.datetime.fromisoformat(w["started_at"])
        finish = dt.datetime.fromisoformat(w["finished_at"])
        assert all(start <= s <= finish for s in stamps)

    # Упражнения — из каталога и на языке аккаунта (английский).
    exercises = (await client.get("/exercises")).json()
    ex_items = exercises["exercises"] if isinstance(exercises, dict) else exercises
    names = {e.get("display_name") or e.get("name") for e in ex_items}
    assert "Bench Press" in names or any("Bench" in (n or "") for n in names), names
    assert not any(any("а" <= ch <= "я" for ch in (n or "").lower()) for n in names), names

    # Значки выданы (achievement_sync.resync отработал по истории).
    achievements = (await client.get("/achievements")).json()
    assert "earned" in str(achievements) or achievements

    # Повторное заполнение не удваивает историю.
    await review_demo.ensure_demo_user()
    assert await fresh_db.count_workouts(user_id) == review_demo.TOTAL_WORKOUTS


@pytest.mark.asyncio
async def test_e1rm_rises_and_records_appear(fresh_db):
    user_id = await review_demo.ensure_demo_user()
    cur = await fresh_db.conn().execute(
        "SELECT w.started_at, MAX(s.weight) AS top FROM sets s "
        "JOIN workout_blocks b ON b.id = s.block_id JOIN workouts w ON w.id = b.workout_id "
        "JOIN exercises e ON e.id = s.exercise_id "
        "WHERE w.user_id = ? AND e.original_name = ? GROUP BY w.id ORDER BY w.started_at",
        (user_id, review_demo.SQUAT),
    )
    tops = [row["top"] for row in await cur.fetchall()]
    assert len(tops) == review_demo.TOTAL_WORKOUTS // 2
    assert tops == sorted(tops) and tops[-1] > tops[0]


@pytest.mark.asyncio
async def test_deleted_demo_account_is_recreated_and_reseeded(fresh_db):
    import account_deletion

    first = await review_demo.ensure_demo_user()
    await account_deletion.delete_account(first)
    second = await review_demo.ensure_demo_user()
    assert await fresh_db.get_user(second) is not None
    assert await fresh_db.count_workouts(second) == review_demo.TOTAL_WORKOUTS
