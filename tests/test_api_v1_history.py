"""REST `/v1` для трёх новых экранов истории (api_v1_history.py):
картинка-визитка тренировки, календарь по месяцам, экспорт CSV.

Тот же приём, что у tests/test_api_v1.py — httpx поверх ASGI, без сокета, с
настоящей проверкой Bearer-токена.
"""

import csv
import io

import httpx
import pytest

import api_v1
import db

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id: int = 111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _finished_workout_with_set(
    user_id: int, *, exercise_name: str = "Жим лёжа", weight: float = 100.0,
    reps: int = 5, started_at: str | None = None,
) -> int:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, exercise_name, group_id)
    workout_id = await db.create_workout(user_id, started_at=started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 1, 0, weight, reps)
    await db.finish_workout(workout_id, finished_at=started_at)
    return workout_id


# ---------- картинка-визитка тренировки ----------

async def test_workout_card_returns_png(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    workout_id = await _finished_workout_with_set(111)

    resp = await client.get(f"/workouts/{workout_id}/card")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    # Магическая сигнатура PNG — не пустой ответ и правда картинка, а не JSON.
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_workout_card_someone_elses_workout_is_404(fresh_db, client_factory):
    owner_workout_id = await _finished_workout_with_set(111)
    other = await _linked_client(fresh_db, client_factory, 222)

    resp = await other.get(f"/workouts/{owner_workout_id}/card")

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


async def test_workout_card_missing_workout_is_404(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)

    resp = await client.get("/workouts/999999/card")

    assert resp.status_code == 404


async def test_workout_card_requires_token(fresh_db, client_factory):
    workout_id = await _finished_workout_with_set(111)
    client = client_factory()

    resp = await client.get(f"/workouts/{workout_id}/card")

    assert resp.status_code == 401


# ---------- календарь истории по месяцам ----------

async def test_calendar_counts_finished_workouts_by_day(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    w1 = await _finished_workout_with_set(111, started_at="2026-03-05T12:00:00")
    w2 = await _finished_workout_with_set(111, started_at="2026-03-05T18:00:00")
    w3 = await _finished_workout_with_set(111, started_at="2026-03-20T09:00:00")
    # За пределами марта — не должна попасть в выдачу.
    await _finished_workout_with_set(111, started_at="2026-04-01T09:00:00")

    resp = await client.get("/workouts/calendar", params={"year": 2026, "month": 3})

    assert resp.status_code == 200, resp.text
    days = resp.json()["days"]
    assert sorted(days["2026-03-05"]) == sorted([w1, w2])
    assert days["2026-03-20"] == [w3]
    assert len(days) == 2


async def test_calendar_empty_month_does_not_crash(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)

    resp = await client.get("/workouts/calendar", params={"year": 2026, "month": 3})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"days": {}}


async def test_calendar_rejects_bad_month(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)

    resp = await client.get("/workouts/calendar", params={"year": 2026, "month": 13})

    assert resp.status_code == 400


async def test_calendar_requires_token(fresh_db, client_factory):
    client = client_factory()

    resp = await client.get("/workouts/calendar", params={"year": 2026, "month": 3})

    assert resp.status_code == 401


# ---------- экспорт CSV ----------

async def test_export_csv_contains_all_sets(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)
    await _finished_workout_with_set(111, exercise_name="Жим лёжа", weight=100.0, reps=5)
    await _finished_workout_with_set(111, exercise_name="Присед", weight=120.0, reps=3)

    resp = await client.get("/export/csv")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["started_at", "exercise", "round_index", "weight", "reps", "rpe"]
    exercises = [r[1] for r in rows[1:]]
    assert exercises == ["Жим лёжа", "Присед"]
    assert len(rows) == 3  # header + два подхода


async def test_export_csv_escapes_commas_in_exercise_name(fresh_db, client_factory):
    """Название упражнения с запятой не должно расклеить строку на два поля —
    csv.writer сам берёт такое значение в кавычки (QUOTE_MINIMAL)."""
    client = await _linked_client(fresh_db, client_factory, 111)
    await _finished_workout_with_set(111, exercise_name="Жим, лёжа узким хватом")

    resp = await client.get("/export/csv")

    text = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1][1] == "Жим, лёжа узким хватом"
    assert len(rows[1]) == 6  # запятая внутри поля не породила лишнюю колонку
    # А в сырых байтах поле действительно взято в кавычки.
    assert '"Жим, лёжа узким хватом"' in text


async def test_export_csv_is_empty_bodied_without_workouts(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, 111)

    resp = await client.get("/export/csv")

    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows == [["started_at", "exercise", "round_index", "weight", "reps", "rpe"]]


async def test_export_csv_requires_token(fresh_db, client_factory):
    client = client_factory()

    resp = await client.get("/export/csv")

    assert resp.status_code == 401
