"""REST `/v1` для дневника еды: запись/чтение дня, суммы, история дней,
владение записями и AI-разбор без сохранения (`/food/parse`).

Гоняется тем же приёмом, что и tests/test_api_v1.py — httpx поверх ASGI, без
сокета, включая настоящую проверку Bearer-токена.
"""

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config


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
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.mark.asyncio
async def test_add_and_read_day_sums_totals(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/food", json={"name": "Овсянка", "kcal": 300, "protein": 10, "fat": 5, "carbs": 40}
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    assert entry["description"] == "Овсянка"
    assert entry["id"] > 0

    # Вторая запись без БЖУ — суммы обязаны сложить только то, что было, а не
    # обнулиться из-за одной записи без раскладки.
    resp = await client.post("/food", json={"name": "Кофе", "kcal": 5})
    assert resp.status_code == 201

    resp = await client.get("/food")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2
    assert body["totals"]["calories"] == 305
    assert body["totals"]["protein"] == 10
    assert body["totals"]["fat"] == 5
    assert body["totals"]["carbs"] == 40


@pytest.mark.asyncio
async def test_add_entry_for_explicit_date(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "Вчерашний ужин", "kcal": 500, "eaten_on": "2020-01-01"})
    assert resp.status_code == 201
    assert resp.json()["eaten_on"] == "2020-01-01"

    # Сегодняшний день (по умолчанию) не должен видеть запись за 2020-01-01.
    today = await client.get("/food")
    assert today.json()["entries"] == []

    old_day = await client.get("/food", params={"date": "2020-01-01"})
    assert len(old_day.json()["entries"]) == 1


@pytest.mark.asyncio
async def test_uses_user_timezone_for_default_date(fresh_db, client_factory):
    """Без ?date отдаётся «сегодня» по tz_offset пользователя, а не по UTC
    сервера — поздний ужин по местному времени не должен улетать во вчера."""
    telegram_id = 111
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    # +14 часов гарантированно сдвигает локальную дату вперёд относительно UTC
    # в любой момент прогона теста.
    await fresh_db.conn().execute(
        "UPDATE users SET tz_offset = 14 WHERE telegram_id = ?", (telegram_id,)
    )
    await fresh_db.conn().commit()

    import datetime as dt

    expected_local_date = (dt.datetime.utcnow() + dt.timedelta(hours=14)).date().isoformat()

    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get("/food")
    assert resp.json()["date"] == expected_local_date


@pytest.mark.asyncio
async def test_delete_entry(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food", json={"name": "Яблоко", "kcal": 80})
    entry_id = resp.json()["id"]

    resp = await client.delete(f"/food/{entry_id}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    resp = await client.get("/food")
    assert resp.json()["entries"] == []

    # Повторное удаление — записи уже нет.
    resp = await client.delete(f"/food/{entry_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_days_history(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/food", json={"name": "День 1", "kcal": 100, "eaten_on": "2024-01-01"})
    await client.post("/food", json={"name": "День 2 (а)", "kcal": 200, "eaten_on": "2024-01-02"})
    await client.post("/food", json={"name": "День 2 (б)", "kcal": 50, "eaten_on": "2024-01-02"})

    resp = await client.get("/food/days")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["days"][0]["date"] == "2024-01-02"  # новее сначала
    assert body["days"][0]["calories"] == 250
    assert body["days"][0]["entries"] == 2
    assert body["days"][1]["date"] == "2024-01-01"


@pytest.mark.asyncio
async def test_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/food")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_bad_date_is_rejected(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/food", params={"date": "not-a-date"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"

    resp = await client.post("/food", json={"name": "Что-то", "kcal": 100, "eaten_on": "31-02-2024"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_read_or_delete_someone_elses_entry(fresh_db, client_factory):
    """Запись адресуется по id, get_food_entry в db.py отдаёт её без проверки
    хозяина — угаданный чужой id не должен ни читаться, ни удаляться."""
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    resp = await owner.post("/food", json={"name": "Чужой обед", "kcal": 400})
    entry_id = resp.json()["id"]

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)

    resp = await intruder.get(f"/food/{entry_id}")
    assert resp.status_code == 404

    resp = await intruder.delete(f"/food/{entry_id}")
    assert resp.status_code == 404

    # Запись у хозяина цела — чужое удаление не должно было пройти.
    still_there = await owner.get(f"/food/{entry_id}")
    assert still_there.status_code == 200


@pytest.mark.asyncio
async def test_parse_returns_503_when_not_configured(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food/parse", json={"text": "тарелка риса"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_parse_requires_text_or_image(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food/parse", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_parse_returns_model_estimate_without_saving(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_analyze_food(user_id, **kwargs):
        assert kwargs["text"] == "тарелка риса"
        return {
            "is_food": True,
            "description": "Рис",
            "items": [],
            "calories": 250,
            "protein": 5,
            "fat": 1,
            "carbs": 55,
        }

    monkeypatch.setattr(ai_trainer, "analyze_food", fake_analyze_food)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food/parse", json={"text": "тарелка риса"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "Рис"

    # /food/parse не пишет в дневник — только показывает догадку.
    day = await client.get("/food")
    assert day.json()["entries"] == []


# ---------- цель по калориям ----------

@pytest.mark.asyncio
async def test_set_kcal_goal_saved_and_visible_on_day(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/food/goal", json={"goal": 2200})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kcal_goal": 2200}

    # GET /food уже читал цель — теперь есть чем её туда положить.
    day = await client.get("/food")
    assert day.json()["kcal_goal"] == 2200


@pytest.mark.asyncio
async def test_set_kcal_goal_can_be_cleared(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/food/goal", json={"goal": 2200})

    resp = await client.post("/food/goal", json={"goal": None})
    assert resp.status_code == 200
    assert resp.json() == {"kcal_goal": None}
    assert (await client.get("/food")).json()["kcal_goal"] is None


@pytest.mark.asyncio
async def test_set_kcal_goal_rejects_out_of_range(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    too_low = await client.post("/food/goal", json={"goal": config.KCAL_GOAL_MIN - 1})
    assert too_low.status_code == 400
    assert too_low.json()["error"] == "out_of_range"

    too_high = await client.post("/food/goal", json={"goal": config.KCAL_GOAL_MAX + 1})
    assert too_high.status_code == 400
    assert too_high.json()["error"] == "out_of_range"

    # Цель не должна была поменяться ни одной из отклонённых попыток.
    assert (await client.get("/food")).json()["kcal_goal"] is None


@pytest.mark.asyncio
async def test_set_kcal_goal_rejects_non_int(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/food/goal", json={"goal": "2200"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_kcal_goal_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/food/goal", json={"goal": 2200})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_parse_respects_daily_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    # 0 отключает лимит вовсе (limit > 0 в ai_limits._exhausted) — нужен именно
    # уже выбранный лимит 1, а не выключенный.
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    ai_limits.reset_cache()

    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.increment_ai_food_count(111)

    resp = await client.post("/food/parse", json={"text": "что угодно"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "food_limit_exceeded"
