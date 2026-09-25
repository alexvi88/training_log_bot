"""Повтор finish (потерянный ответ, офлайн-очередь, двойной тап) застаёт
AI-комментарий, заказанный первым вызовом, ещё в полёте. Раньше повтор всегда
отвечал ai_comment_pending=false, и приложение (FinishResponse.
shouldAwaitAIComment) переставало ждать комментарий, который вот-вот появится.
"""

import asyncio

import httpx
import pytest

import ai_trainer
import api_v1
import config

H = {config.AI_CONSENT_CLIENT_HEADER: "1"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked(fresh_db, tid=111):
    await fresh_db.get_or_create_user(telegram_id=tid, username="t")
    await fresh_db.update_user(tid, ai_comments_enabled=1)
    code = await fresh_db.issue_oauth_link_code(tid, ttl_seconds=600, digits=8)
    c = _client()
    r = await c.post("/auth/link", json={"code": code})
    c.headers["Authorization"] = f"Bearer {r.json()['token']}"
    assert (await c.patch("/settings", json={"ai_consent": True})).status_code == 200
    return c


@pytest.fixture
def gated_model(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    st = {"calls": [], "gate": asyncio.Event()}

    async def fake_comment(user_id, workout_id):
        st["calls"].append(workout_id)
        await st["gate"].wait()
        return "Молодец."

    monkeypatch.setattr(ai_trainer, "comment_on_workout", fake_comment)
    return st


async def _open_workout(c):
    wid = (await c.post("/workouts/active")).json()["id"]
    eid = (await c.post("/exercises", json={"name": "Жим"})).json()["id"]
    await c.post(f"/workouts/{wid}/sets", json={"exercise_id": eid, "weight": 50, "reps": 5})
    return wid


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0)
    if api_v1._ai_comment_tasks:
        await asyncio.gather(*list(api_v1._ai_comment_tasks))


async def test_replay_while_comment_in_flight_is_pending(fresh_db, gated_model):
    c = await _linked(fresh_db)
    wid = await _open_workout(c)
    r1 = await c.post(f"/workouts/{wid}/finish", json={}, headers=H)
    assert r1.json()["ai_comment_pending"] is True
    await asyncio.sleep(0)
    assert wid in api_v1._ai_comment_inflight

    r2 = await c.post(f"/workouts/{wid}/finish", json={}, headers=H)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["ai_comment_pending"] is True

    gated_model["gate"].set()
    await _drain()
    # Повтор не заказывает второй платный вызов.
    assert gated_model["calls"] == [wid]
    assert (await c.get(f"/workouts/{wid}/ai-comment")).json()["comment"] == "Молодец."

    # Комментарий уже готов — повтор больше не обещает его.
    r3 = await c.post(f"/workouts/{wid}/finish", json={}, headers=H)
    assert r3.json()["replayed"] is True
    assert r3.json()["ai_comment_pending"] is False


async def test_replay_without_generation_is_not_pending(fresh_db, gated_model):
    c = await _linked(fresh_db)
    await fresh_db.update_user(111, ai_comments_enabled=0)
    wid = await _open_workout(c)
    r1 = await c.post(f"/workouts/{wid}/finish", json={}, headers=H)
    assert r1.json()["ai_comment_pending"] is False
    r2 = await c.post(f"/workouts/{wid}/finish", json={}, headers=H)
    assert r2.json()["replayed"] is True
    assert r2.json()["ai_comment_pending"] is False
    assert gated_model["calls"] == []
