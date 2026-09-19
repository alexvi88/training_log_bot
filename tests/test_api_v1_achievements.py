"""REST `/v1` — достижения: список, «ближайшие», статистика.

Фикстуры/приём — как в tests/test_api_v1.py: httpx поверх ASGI-приложения без
сокета, `_linked_client` заводит привязанного пользователя и токен так же,
как это делает настоящий auth-flow (/auth/link).
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
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.mark.asyncio
async def test_list_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/achievements")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_for_fresh_user_has_full_catalog_none_earned(fresh_db, client_factory):
    """У нового пользователя ничего не заработано, но каталог целиком отдаётся
    (сколько значков всего — тоже нужно клиенту, не только то, что открыто)."""
    import achievements

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/achievements")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == len(achievements.CATALOG)
    assert all(a["earned"] is False and a["earned_at"] is None for a in body)
    # Текст пришёл локализованным, а не голым кодом.
    first = next(a for a in body if a["code"] == "first")
    assert first["title"]
    assert first["description"]


@pytest.mark.asyncio
async def test_list_reflects_awarded_achievements(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    awarded = await db.award_achievements(111, {"first", "w10"})
    assert set(awarded) == {"first", "w10"}

    resp = await client.get("/achievements")
    body = resp.json()
    by_code = {a["code"]: a for a in body}
    assert by_code["first"]["earned"] is True
    assert by_code["first"]["earned_at"] is not None
    assert by_code["w10"]["earned"] is True
    assert by_code["w25"]["earned"] is False
    assert by_code["w25"]["earned_at"] is None


@pytest.mark.asyncio
async def test_list_does_not_leak_other_users_achievements(fresh_db, client_factory):
    """Второй пользователь не должен увидеть чужой заработанный значок в своём
    списке (и наоборот) — earned считается персонально по токену."""
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    await db.award_achievements(111, {"first"})

    resp_a = await client_a.get("/achievements")
    resp_b = await client_b.get("/achievements")
    by_code_a = {a["code"]: a for a in resp_a.json()}
    by_code_b = {a["code"]: a for a in resp_b.json()}
    assert by_code_a["first"]["earned"] is True
    assert by_code_b["first"]["earned"] is False


@pytest.mark.asyncio
async def test_list_localizes_by_user_lang(fresh_db, client_factory):
    """Один и тот же код достижения — разный текст для разных lang, без
    хардкода строк в модуле (тексты берутся из locales/*.json через i18n)."""
    await fresh_db.get_or_create_user(telegram_id=111, username="ru_user")
    await fresh_db.get_or_create_user(telegram_id=222, username="en_user")
    await fresh_db.update_user(222, lang="en")

    client_ru = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_en = client_factory()
    code = await fresh_db.issue_oauth_link_code(222, ttl_seconds=600, digits=8)
    resp = await client_en.post("/auth/link", json={"code": code})
    client_en.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    ru_title = {a["code"]: a["title"] for a in (await client_ru.get("/achievements")).json()}["first"]
    en_title = {a["code"]: a["title"] for a in (await client_en.get("/achievements")).json()}["first"]
    assert ru_title != en_title


@pytest.mark.asyncio
async def test_nearest_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/achievements/nearest")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_nearest_reports_progress_toward_unearned_badge(fresh_db, client_factory, monkeypatch):
    """Логика "что ближе всего" не пишется заново — переиспользуем
    achievements.nearest_progress (та же функция, что кроет
    tests/test_achievements_nearest.py), эндпоинт лишь сериализует её вывод."""
    import achievement_sync
    import achievements

    client = await _linked_client(fresh_db, client_factory)

    async def fake_ctx(user_id):
        return achievements.AchievementContext(
            total_workouts=9,  # 1 подход до "w10"
            lifetime_tonnage_kg=0.0,
            best_week_streak=0,
            max_weight_kg=0.0,
            distinct_exercises=0,
        )

    monkeypatch.setattr(achievement_sync, "aggregate_context", fake_ctx)

    resp = await client.get("/achievements/nearest?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["code"] == "w10"
    assert body[0]["current"] == 9
    assert body[0]["target"] == 10
    assert body[0]["remaining"] == 1
    assert body[0]["title"]


@pytest.mark.asyncio
async def test_stats_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/achievements/stats")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stats_returns_extremes_for_fresh_user(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/achievements/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "max_sets": 0,
        "max_tonnage": 0,
        "max_exercises": 0,
        "max_bw_reps": 0,
        "distinct_groups": 0,
        "has_superset": False,
        "early_workouts": 0,
    }
