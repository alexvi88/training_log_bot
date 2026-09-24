"""`/v1`: пояс атлета и повторы запросов — находки аудита, каждая падала до
правки.

- аккаунт из Sign in with Apple заводился с чужим поясом (+3), а приложение не
  умело подтянуть пояс телефона, не затирая выбранный руками;
- занесение задним числом и точки графика прогресса путали UTC-день с местным;
- добавление упражнения, которое уже есть в дне, отвечало 500;
- повтор «Завершить» после оборванного ответа получал 409 вместо итогов;
- вес тела принимал 0, минус и 10^9, а метку времени — любую строку;
- 413 на слишком большое тело отдавал английский текст всем.
"""

import datetime as dt

import httpx
import pytest

import api_v1
import api_v1_common
import body_limit
import timeutil


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _fake_apple(monkeypatch, apple_user_id="apple-tz"):
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=apple_user_id, email=None),
    )


# ---------- 1. пояс нового аккаунта и пояс телефона ----------


def test_device_offset_rounds_to_whole_hours():
    assert api_v1_common.device_tz_offset_hours(180) == 3
    assert api_v1_common.device_tz_offset_hours(330) == 6  # Индия, +5:30
    assert api_v1_common.device_tz_offset_hours(345) == 6  # Непал, +5:45
    assert api_v1_common.device_tz_offset_hours(-210) == -3  # Ньюфаундленд, -3:30
    assert api_v1_common.device_tz_offset_hours(840) == 14
    assert api_v1_common.device_tz_offset_hours(-720) == -11  # за краем пикера
    assert api_v1_common.device_tz_offset_hours(900) is None
    assert api_v1_common.device_tz_offset_hours("180") is None
    assert api_v1_common.device_tz_offset_hours(True) is None


@pytest.mark.asyncio
async def test_apple_signup_takes_phone_timezone(fresh_db, client_factory, monkeypatch):
    _fake_apple(monkeypatch)
    client = client_factory()
    resp = await client.post(
        "/auth/apple", json={"identity_token": "t", "lang": "en", "tz_offset_minutes": -300}
    )
    assert resp.status_code == 200, resp.text
    user = await fresh_db.get_user(resp.json()["user_id"])
    assert user["tz_offset"] == -5
    # Не выбран человеком — приложению можно поправить его позже.
    assert user["tz_set_by_user"] == 0


@pytest.mark.asyncio
async def test_apple_signup_without_timezone_keeps_default(fresh_db, client_factory, monkeypatch):
    """Старые сборки поле не шлют — всё как раньше; мусор вход не срывает."""
    import config

    _fake_apple(monkeypatch)
    resp = await client_factory().post(
        "/auth/apple", json={"identity_token": "t", "tz_offset_minutes": "garbage"}
    )
    assert resp.status_code == 200, resp.text
    user = await fresh_db.get_user(resp.json()["user_id"])
    assert user["tz_offset"] == config.DEFAULT_TZ_OFFSET


@pytest.mark.asyncio
async def test_device_timezone_patch_applies_until_user_picks_one(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    settings = (await client.get("/settings")).json()
    assert settings["tz_set_by_user"] is False

    resp = await client.patch("/settings", json={"device_tz_offset_minutes": 600})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tz_offset"] == 10
    assert resp.json()["tz_set_by_user"] is False

    # Человек выбрал пояс сам — пояс телефона больше его не трогает.
    picked = await client.patch("/settings", json={"tz_offset": 2})
    assert picked.json()["tz_set_by_user"] is True
    ignored = await client.patch("/settings", json={"device_tz_offset_minutes": 600})
    assert ignored.status_code == 200
    assert ignored.json()["tz_offset"] == 2


@pytest.mark.asyncio
async def test_device_timezone_patch_rejects_garbage(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.patch("/settings", json={"device_tz_offset_minutes": 10_000})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


# ---------- 2. занесение задним числом у UTC+13/+14 ----------


def test_backdated_moment_lands_on_the_same_day_locally_and_in_utc():
    day = dt.date(2026, 9, 8)
    for offset in range(-11, 15):
        stored = dt.datetime.fromisoformat(timeutil.backdated_moment(day, offset))
        assert stored.date() == day, offset
        assert (stored + dt.timedelta(hours=offset)).date() == day, offset
    assert timeutil.backdated_moment(day, 0) == "2026-09-08T12:00:00"
    assert timeutil.logged_at_for_date(day) == "2026-09-08T12:00:00"


@pytest.mark.asyncio
async def test_backfill_at_plus_13_stays_on_the_picked_day(fresh_db, client_factory):
    """Голый полдень UTC у UTC+13 — это 01:00 следующих суток по местному:
    тренировка за вторник вставала в историю и календарь средой."""
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, tz_offset=13)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    started = await client.post("/workouts/backfill", json={"date": "2026-09-08"})
    assert started.status_code == 201, started.text
    workout_id = started.json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )
    finished = await client.post(f"/workouts/{workout_id}/finish")
    assert finished.status_code == 200, finished.text

    assert await fresh_db.list_finished_workout_dates(111) == ["2026-09-08"]
    workout = await fresh_db.get_workout(workout_id)
    # Сырая дата строки тоже та же — её читают старые сборки приложения.
    assert workout["started_at"][:10] == "2026-09-08"
    assert workout["finished_at"] == workout["started_at"]


# ---------- 3. точка графика прогресса — местный день ----------


@pytest.mark.asyncio
async def test_progress_point_date_is_the_local_day(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, tz_offset=-5)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    # 02:00 UTC 10-го — это 21:00 9-го по UTC-5.
    workout_id = await fresh_db.create_finished_workout(
        111, "2026-09-10T02:00:00", "2026-09-10T03:00:00"
    )
    block_id = await fresh_db.create_block(workout_id, "single")
    await fresh_db.add_block_exercise(block_id, exercise_id, 0)
    await fresh_db.add_set(block_id, exercise_id, 1, 0, 100, 5, None)

    resp = await client.get(f"/exercises/{exercise_id}/progress/sessions")
    assert resp.status_code == 200, resp.text
    assert [p["date"] for p in resp.json()["points"]] == ["2026-09-09"]


# ---------- 4. упражнение, которое уже есть в дне ----------


@pytest.mark.asyncio
async def test_adding_exercise_already_in_day_returns_existing(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    routine_id = (await client.post("/routines", json={"name": "День А"})).json()["id"]
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    first = await client.post(f"/routines/{routine_id}/exercises", json={"exercise_id": exercise_id})
    assert first.status_code == 201, first.text
    again = await client.post(
        f"/routines/{routine_id}/exercises", json={"exercise_id": exercise_id, "target": "5×5"}
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert len(await fresh_db.list_routine_exercises(routine_id)) == 1


# ---------- 6. повтор «Завершить» ----------


@pytest.mark.asyncio
async def test_finish_retry_returns_summary_instead_of_409(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )

    first = await client.post(f"/workouts/{workout_id}/finish")
    assert first.status_code == 200, first.text
    retry = await client.post(f"/workouts/{workout_id}/finish")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["id"] == workout_id
    assert body["status"] == "finished"
    assert body["replayed"] is True
    rewards = body["rewards"]
    assert rewards["sets"] == first.json()["rewards"]["sets"] == 1
    assert rewards["tonnage"] == first.json()["rewards"]["tonnage"]
    # События момента финиша второй раз не объявляются.
    assert rewards["new_achievements"] == []
    assert rewards["rank_promotion"] is None
    assert rewards["milestone"] is None


@pytest.mark.asyncio
async def test_finish_of_someone_elses_workout_is_still_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    workout_id = (await owner.post("/workouts/active")).json()["id"]
    stranger = await _linked_client(fresh_db, client_factory, telegram_id=222)
    resp = await stranger.post(f"/workouts/{workout_id}/finish")
    assert resp.status_code == 404


# ---------- 7. вес тела ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [0, -80, 1e9, True])
async def test_bodyweight_rejects_impossible_weight(fresh_db, client_factory, weight):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/bodyweight", json={"weight": weight})
    assert resp.status_code == 400, resp.text
    assert await fresh_db.list_bodyweight_logs(111) == []


@pytest.mark.asyncio
async def test_bodyweight_update_rejects_impossible_weight(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    log_id = (await client.post("/bodyweight", json={"weight": 82.5})).json()["id"]
    resp = await client.patch(f"/bodyweight/{log_id}", json={"weight": 0})
    assert resp.status_code == 400
    assert resp.json()["message"]
    assert (await fresh_db.list_bodyweight_logs(111))[0]["weight"] == 82.5


@pytest.mark.asyncio
@pytest.mark.parametrize("logged_at", ["вчера", "2026-13-40T12:00:00", "2999-01-01T12:00:00", 5])
async def test_bodyweight_rejects_bad_logged_at(fresh_db, client_factory, logged_at):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/bodyweight", json={"weight": 82.5, "logged_at": logged_at})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_bodyweight_accepts_naive_and_zoned_logged_at(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    naive = await client.post("/bodyweight", json={"weight": 82.5, "logged_at": "2026-08-01T12:00:00"})
    assert naive.status_code == 201, naive.text
    assert naive.json()["logged_at"] == "2026-08-01T12:00:00"
    zoned = await client.post(
        "/bodyweight", json={"weight": 82.5, "logged_at": "2026-08-01T12:00:00+03:00"}
    )
    assert zoned.status_code == 201, zoned.text
    assert zoned.json()["logged_at"] == "2026-08-01T09:00:00"


# ---------- 9. 413 на языке атлета ----------


async def _reject_413(headers):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def app(scope, receive, send):  # pragma: no cover — сюда не доходит
        raise AssertionError("app must not be called")

    middleware = body_limit.MaxBodySizeMiddleware(app, max_bytes=10)
    await middleware({"type": "http", "method": "POST", "path": "/v1/ai/ask", "headers": headers}, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body.decode("utf-8")


@pytest.mark.asyncio
async def test_413_message_follows_accept_language():
    import i18n

    status, body = await _reject_413([(b"accept-language", b"ru-RU,ru;q=0.9")])
    assert status == 413
    assert i18n.t_in("ru", "api.error.payload_too_large") in body
    _, body_en = await _reject_413([(b"accept-language", b"en-US")])
    assert i18n.t_in("en", "api.error.payload_too_large") in body_en
    assert "request body too large" in body_en  # машинный detail остался


@pytest.mark.asyncio
async def test_413_message_follows_account_language(fresh_db, client_factory):
    import i18n

    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.set_user_lang(111, "en")
    token = client.headers["Authorization"].encode()
    _, body = await _reject_413([(b"authorization", token), (b"accept-language", b"ru")])
    assert i18n.t_in("en", "api.error.payload_too_large") in body
