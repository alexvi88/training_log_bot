"""Строка рекорда 🔥 в JSON тренировки (GET /v1/workouts/{id} и ответ finish).

Приложение не строит эту фразу сама — вторая реализация разъехалась бы с
formatting.format_block_record молча (CLAUDE.md). Проверяем, что REST-слой
зовёт ровно ту же пару view_builder.build_block_views(mark_records=True) +
formatting.format_block_record, что и карточка бота, а не собирает своё.

По образцу tests/test_api_v1_finish_rewards.py: httpx поверх ASGI-приложения
без сокета, `client_factory` и `_linked_client` заведены локально — в чужой
тестовый файл не лезем, его может в это же время править другой агент.
"""

import re

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
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


_CYRILLIC = re.compile("[а-яёА-ЯЁ]")


def _exercise_entry(body: dict, exercise_id: int) -> dict:
    for block in body["blocks"]:
        for ex in block["exercises"]:
            if ex["exercise_id"] == exercise_id:
                return ex
    raise AssertionError(f"exercise {exercise_id} not found in {body['blocks']}")


async def _log_set(client, workout_id: int, exercise_id: int, weight: float, reps: int):
    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": weight, "reps": reps},
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_exercise_with_e1rm_record_gets_ready_text(fresh_db, client_factory):
    """Второй сеанс с более тяжёлым подходом бьёт e1RM первого — та же строка,
    что бот печатает «🔥 +Nкг к рекорду» (formatting._block_record_text)."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    first = (
        await client.post("/workouts/backfill", json={"date": "2024-01-01"})
    ).json()["id"]
    await _log_set(client, first, exercise_id, 80, 5)
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, second, exercise_id, 120, 5)
    resp = await client.post(f"/workouts/{second}/finish", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    entry = _exercise_entry(body, exercise_id)
    assert entry["has_record"] is True
    assert entry["record_text"]
    assert "🔥" in entry["record_text"]

    # Тот же ответ отдаёт и обычный GET тренировки, не только finish.
    get_body = (await client.get(f"/workouts/{second}")).json()
    assert _exercise_entry(get_body, exercise_id) == entry


@pytest.mark.asyncio
async def test_exercise_without_record_reports_null(fresh_db, client_factory):
    """Первая в жизни сессия упражнения рекорда не даёт (view_builder.
    _session_record) — бить ещё нечего, `record_text` держит `null`."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 80, 5)

    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["has_record"] is False
    assert entry["record_text"] is None


@pytest.mark.asyncio
async def test_show_extra_stats_off_hides_e1rm_record_but_not_reps_record(
    fresh_db, client_factory
):
    """Ключевая тонкость из докстринга formatting.format_block_record: рекорд
    e1RM молчит при выключенных доп. цифрах (нельзя получить e1RM через
    заднюю дверь), а рекорд повторов своим весом виден всегда."""
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"show_extra_stats": False})

    weighted = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    bodyweight = (await client.post("/exercises", json={"name": "Подтягивания"})).json()["id"]

    first = (
        await client.post("/workouts/backfill", json={"date": "2024-01-01"})
    ).json()["id"]
    await _log_set(client, first, weighted, 80, 5)
    await _log_set(client, first, bodyweight, 0, 6)
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, second, weighted, 120, 5)  # побил бы e1RM
    await _log_set(client, second, bodyweight, 0, 9)  # бьёт рекорд повторов
    body = (await client.post(f"/workouts/{second}/finish", json={})).json()

    e1rm_entry = _exercise_entry(body, weighted)
    assert e1rm_entry["has_record"] is False
    assert e1rm_entry["record_text"] is None

    reps_entry = _exercise_entry(body, bodyweight)
    assert reps_entry["has_record"] is True
    assert reps_entry["record_text"]
    assert "🔥" in reps_entry["record_text"]


@pytest.mark.asyncio
async def test_record_text_speaks_users_language(fresh_db, client_factory):
    """Без i18n.use_lang язык строки был бы тем, который первым дёрнул модуль
    в этом процессе (CLAUDE.md, «Ловушка, встретившаяся шесть раз»)."""
    client = await _linked_client(fresh_db, client_factory)
    await client.patch("/settings", json={"lang": "en"})
    bodyweight = (await client.post("/exercises", json={"name": "Pull-ups"})).json()["id"]

    first = (
        await client.post("/workouts/backfill", json={"date": "2024-01-01"})
    ).json()["id"]
    await _log_set(client, first, bodyweight, 0, 6)
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, second, bodyweight, 0, 9)
    body = (await client.post(f"/workouts/{second}/finish", json={})).json()

    entry = _exercise_entry(body, bodyweight)
    assert entry["record_text"]
    assert not _CYRILLIC.search(entry["record_text"]), entry["record_text"]


# ---------- previous_sets_text ----------


@pytest.mark.asyncio
async def test_previous_sets_text_shows_last_sessions_sets(fresh_db, client_factory):
    """Второй сеанс того же упражнения получает подходы первого — тот же
    формат (formatting.format_set), что бот печатает под блоком «[прошлая: …]»."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    first = (
        await client.post("/workouts/backfill", json={"date": "2024-01-01"})
    ).json()["id"]
    await _log_set(client, first, exercise_id, 80, 5)
    await _log_set(client, first, exercise_id, 80, 4)
    await client.post(f"/workouts/{first}/finish", json={})

    second = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, second, exercise_id, 82.5, 5)
    body = (await client.post(f"/workouts/{second}/finish", json={})).json()

    entry = _exercise_entry(body, exercise_id)
    assert entry["previous_sets_text"] == "80×5, 80×4"

    # Тот же ответ отдаёт и обычный GET тренировки, не только finish.
    get_body = (await client.get(f"/workouts/{second}")).json()
    assert _exercise_entry(get_body, exercise_id)["previous_sets_text"] == "80×5, 80×4"


@pytest.mark.asyncio
async def test_previous_sets_text_null_on_first_ever_session(fresh_db, client_factory):
    """Первая в жизни сессия упражнения — сравнивать не с чем, `null`, а не
    пустая строка или падение."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await _log_set(client, workout_id, exercise_id, 80, 5)

    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()
    entry = _exercise_entry(body, exercise_id)
    assert entry["previous_sets_text"] is None
