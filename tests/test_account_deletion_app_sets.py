"""Удаление аккаунта у того, кто записывал подходы из приложения.

Каждый подход из приложения приходит с ключом идемпотентности, и под него
пишется строка `set_write_attempts` с FK на `sets(id)` без каскада. Снос
аккаунта стирал `sets` раньше этих строк и падал «FOREIGN KEY constraint
failed»: `DELETE /v1/account` отвечал 500 и не удалял ничего, хотя токены
Sign in with Apple к этому моменту уже были отозваны.
"""

import httpx
import pytest

import api_v1


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def test_account_with_app_logged_sets_is_deleted(fresh_db, client_factory):
    telegram_id = 111
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    workout_id = (await client.post("/workouts/active")).json()["id"]
    exercise_id = (await client.post("/exercises", json={"name": "Жим"})).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 5, "idempotency_key": "k1"},
    )
    assert resp.status_code in (200, 201), resp.text
    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "100 8, 90 8", "idempotency_key": "k2"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert (await client.post(f"/workouts/{workout_id}/finish", json={})).status_code == 200

    resp = await client.delete("/account?confirm=delete")
    assert resp.status_code == 200, resp.text
    assert await fresh_db.user_data_left(telegram_id) == {}
