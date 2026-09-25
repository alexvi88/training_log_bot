"""Анонимная воронка до входа (`POST /funnel`, api_v1_funnel.py): белый список
шагов, строка на пару (установка, шаг), лимиты частоты и размера тела, без PII."""

from __future__ import annotations

import uuid

import httpx
import pytest

import api_v1
import api_v1_funnel

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_limits():
    api_v1_funnel.reset_limits()
    yield
    api_v1_funnel.reset_limits()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _rows(fresh_db):
    cur = await fresh_db.conn().execute("SELECT * FROM funnel_events ORDER BY id")
    return await cur.fetchall()


async def test_step_is_recorded_without_auth_and_only_once_per_install(fresh_db):
    install = str(uuid.uuid4())
    client = _client()
    first = await client.post("/funnel", json={"install_id": install, "step": "onboarding_slide_1", "lang": "en-US"})
    assert first.status_code == 200, first.text
    assert first.json() == {"recorded": True}
    again = await client.post("/funnel", json={"install_id": install.upper(), "step": "onboarding_slide_1"})
    assert again.json() == {"recorded": False}
    rows = await _rows(fresh_db)
    assert len(rows) == 1
    assert rows[0]["install_id"] == install
    assert rows[0]["step"] == "onboarding_slide_1"
    assert rows[0]["lang"] == "en"
    # Без PII: в строке нет ничего, кроме установки, шага, языка и времени.
    assert set(rows[0].keys()) == {"id", "install_id", "step", "lang", "created_at"}


async def test_every_whitelisted_step_is_accepted(fresh_db):
    install = str(uuid.uuid4())
    client = _client()
    for step in sorted(api_v1_funnel.STEPS):
        resp = await client.post("/funnel", json={"install_id": install, "step": step})
        assert resp.status_code == 200, (step, resp.text)
    assert len(await _rows(fresh_db)) == len(api_v1_funnel.STEPS)


@pytest.mark.parametrize(
    "body",
    [
        {"install_id": "not-a-uuid", "step": "signin_ok"},
        {"install_id": 42, "step": "signin_ok"},
        {"step": "signin_ok"},
        {"install_id": "8e7b0c9e-5b2a-4f4e-9d0a-2f1b8b2c3d4e", "step": "drop table"},
        {"install_id": "8e7b0c9e-5b2a-4f4e-9d0a-2f1b8b2c3d4e"},
    ],
)
async def test_bad_body_is_rejected_and_nothing_is_written(fresh_db, body):
    resp = await _client().post("/funnel", json=body)
    assert resp.status_code == 400
    assert await _rows(fresh_db) == []


async def test_oversized_body_is_rejected(fresh_db):
    body = {"install_id": str(uuid.uuid4()), "step": "signin_ok", "pad": "x" * 2000}
    resp = await _client().post("/funnel", json=body)
    assert resp.status_code == 400
    assert await _rows(fresh_db) == []


async def test_per_ip_rate_limit(fresh_db, monkeypatch):
    monkeypatch.setattr(api_v1_funnel, "LIMIT_PER_IP", 3)
    client = _client()
    codes = []
    for _ in range(5):
        resp = await client.post("/funnel", json={"install_id": str(uuid.uuid4()), "step": "signin_shown"})
        codes.append(resp.status_code)
    assert codes == [200, 200, 200, 429, 429]
    # Другой адрес — свой счётчик.
    other = await client.post(
        "/funnel",
        json={"install_id": str(uuid.uuid4()), "step": "signin_shown"},
        headers={"Fly-Client-IP": "203.0.113.9"},
    )
    assert other.status_code == 200
    assert len(await _rows(fresh_db)) == 4


async def test_total_rate_limit_across_addresses(fresh_db, monkeypatch):
    monkeypatch.setattr(api_v1_funnel, "LIMIT_TOTAL", 2)
    client = _client()
    codes = []
    for i in range(3):
        resp = await client.post(
            "/funnel",
            json={"install_id": str(uuid.uuid4()), "step": "signin_shown"},
            headers={"Fly-Client-IP": f"203.0.113.{i}"},
        )
        codes.append(resp.status_code)
    assert codes == [200, 200, 429]


async def test_funnel_events_are_pruned_with_the_activity_log(fresh_db):
    await fresh_db.log_funnel_event(str(uuid.uuid4()), "signin_ok", "ru")
    await fresh_db.conn().execute("UPDATE funnel_events SET created_at = '2000-01-01T00:00:00'")
    await fresh_db.conn().commit()
    await fresh_db.log_funnel_event(str(uuid.uuid4()), "signin_ok", "ru")
    assert await fresh_db.prune_old_funnel_events(30) == 1
    assert len(await _rows(fresh_db)) == 1


async def test_growth_shows_app_funnel_by_step(fresh_db):
    import acquisition

    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    for step in ("onboarding_slide_1", "onboarding_slide_3", "signin_ok"):
        await fresh_db.log_funnel_event(a, step, "en")
    await fresh_db.log_funnel_event(b, "onboarding_slide_1", "ru")
    text = acquisition.format_app_funnel(await fresh_db.app_funnel(30), 30)
    assert "слайд 1: 2 (100%) (en 1, ru 1)" in text
    assert "слайд 3: 1 (50%) (en 1)" in text
    assert "вошёл: 1 (50%) (en 1)" in text
    assert "слайд 2 (локскрин, iOS 17+): 0 (0%)" in text
    empty = acquisition.format_app_funnel([], 30)
    assert "не было" in empty
