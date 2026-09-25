"""REST `/v1` для импорта истории тренировок из CSV (api_v1_import.py).

Гоняется тем же приёмом, что tests/test_api_v1.py и test_api_v1_food.py —
httpx поверх ASGI, без сокета, с настоящей проверкой Bearer-токена.
"""

import httpx
import pytest

import ai_trainer
import api_v1

pytestmark = pytest.mark.asyncio


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


CSV_TWO_WORKOUTS = (
    "date,exercise,weight,reps\n"
    "2024-01-01,Присед,100,5\n"
    "2024-01-01,Присед,100,5\n"
    "2024-01-03,Жим лёжа,80,8\n"
)


@pytest.fixture(autouse=True)
def _no_ai_matching(monkeypatch):
    # Матчинг чужих названий через модель — сетевой вызов; в этих тестах
    # катлог всегда точно совпадает по имени (см. CSV выше), либо имя новое и
    # намеренно должно завестись без шаблона — ai_trainer.is_configured=False
    # даёт match_exercise_names_to_catalog вернуть {} без похода в сеть (см.
    # её докстринг).
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


async def test_preview_reports_counts_and_marks_known_vs_new_exercises(fresh_db, client_factory):
    user_id = 111
    await fresh_db.get_or_create_user(telegram_id=user_id, username="tester")
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    await fresh_db.create_exercise(user_id, "Присед", gid)
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)

    resp = await client.post("/import/csv/preview", json={"csv": CSV_TWO_WORKOUTS})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workout_count"] == 2
    assert body["set_count"] == 3
    assert body["date_range"] == {"from": "2024-01-01", "to": "2024-01-03"}
    by_name = {e["name"]: e["status"] for e in body["exercises"]}
    assert by_name["Присед"] == "existing"
    assert by_name["Жим лёжа"] == "new_will_create"


async def test_preview_does_not_write_to_the_database(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/import/csv/preview", json={"csv": CSV_TWO_WORKOUTS})
    assert resp.status_code == 200, resp.text

    workouts = await fresh_db.list_workouts(111, limit=10, offset=0, status="finished")
    assert workouts == []
    # Упражнение "Присед" отсутствовало в каталоге — preview не должен был
    # завести его, хотя пометил "new_will_create".
    assert await fresh_db.find_exercise_by_name(111, "Присед") is None


async def test_import_creates_workouts_and_sets(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/import/csv", json={"csv": CSV_TWO_WORKOUTS})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workouts_imported"] == 2
    assert body["sets_imported"] == 3
    assert body["workouts_skipped_duplicate"] == 0
    assert body["workouts_failed"] == 0

    workouts = await fresh_db.list_workouts(111, limit=10, offset=0, status="finished")
    assert len(workouts) == 2
    # Упражнения без точного совпадения в каталоге заводятся сами
    # (create_missing_exercises по умолчанию true).
    assert await fresh_db.find_exercise_by_name(111, "Присед") is not None
    assert await fresh_db.find_exercise_by_name(111, "Жим лёжа") is not None


async def test_import_without_create_missing_skips_unknown_exercises(fresh_db, client_factory):
    user_id = 111
    await fresh_db.get_or_create_user(telegram_id=user_id, username="tester")
    gid = await fresh_db.create_muscle_group(user_id, "Ноги")
    await fresh_db.create_exercise(user_id, "Присед", gid)
    client = await _linked_client(fresh_db, client_factory, telegram_id=user_id)

    resp = await client.post(
        "/import/csv", json={"csv": CSV_TWO_WORKOUTS, "create_missing_exercises": False}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Только тренировка 2024-01-01 (Присед, уже в каталоге) должна пройти;
    # 2024-01-03 (Жим лёжа, неизвестное имя) пропускается целиком.
    assert body["workouts_imported"] == 1
    assert body["sets_imported"] == 2
    assert body["workouts_skipped_unresolved_exercise"] == 1
    assert body["skipped_exercises"] == ["Жим лёжа"]
    assert await fresh_db.find_exercise_by_name(user_id, "Жим лёжа") is None


async def test_reimporting_the_same_file_does_not_duplicate_history(fresh_db, client_factory):
    """Повторная заливка того же файла не должна удваивать историю — то же
    поведение, что у кнопки "✅ Загрузить" в боте (см. handlers.csv_import.
    _duplicate_dates): дата с уже записанной тренировкой того же упражнения
    молча пропускается, а не импортируется заново."""
    client = await _linked_client(fresh_db, client_factory)
    first = await client.post("/import/csv", json={"csv": CSV_TWO_WORKOUTS})
    assert first.json()["workouts_imported"] == 2

    second = await client.post("/import/csv", json={"csv": CSV_TWO_WORKOUTS})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["workouts_imported"] == 0
    assert body["workouts_skipped_duplicate"] == 2

    workouts = await fresh_db.list_workouts(111, limit=10, offset=0, status="finished")
    assert len(workouts) == 2  # не 4


async def test_import_rejects_malformed_csv_with_line_number_in_english(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    bad_csv = (
        "date,exercise,weight,reps\n"
        "2024-01-01,Присед,100,5\n"
        "2024-01-02,Жим лёжа,-50,8\n"  # отрицательный вес — строка 3
    )
    resp = await client.post("/import/csv", json={"csv": bad_csv})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_csv"
    # Машинная причина (`detail`) — по-английски (см. locales/en.json), а не
    # русская строка; человеческое `message` — на языке атлета (здесь ru) и с
    # номером строки, тем же текстом, что увидел бы в боте.
    assert "negative weight" in body["detail"]
    assert "Строка" not in body["detail"]
    assert "Строка 3" in body["message"]

    resp2 = await client.post("/import/csv/preview", json={"csv": bad_csv})
    assert resp2.status_code == 400
    assert resp2.json()["error"] == "invalid_csv"


async def test_import_rejects_oversized_csv(fresh_db, client_factory, monkeypatch):
    import api_v1_import

    monkeypatch.setattr(api_v1_import, "MAX_CSV_BYTES", 100)
    client = await _linked_client(fresh_db, client_factory)
    huge_csv = "date,exercise,weight,reps\n" + "2024-01-01,Присед,100,5\n" * 20
    assert len(huge_csv) > 100

    resp = await client.post("/import/csv", json={"csv": huge_csv})
    assert resp.status_code == 413
    assert resp.json()["error"] == "csv_too_large"


async def test_import_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/import/csv", json={"csv": CSV_TWO_WORKOUTS})
    assert resp.status_code == 401

    resp2 = await client.post("/import/csv/preview", json={"csv": CSV_TWO_WORKOUTS})
    assert resp2.status_code == 401


async def test_preview_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/import/csv/preview", json={"csv": CSV_TWO_WORKOUTS})
    assert resp.status_code == 401


async def test_bom_prefixed_csv_is_read_like_a_plain_one(fresh_db, client_factory):
    """CSV из Excel/Numbers начинается с BOM (U+FEFF). Бот снимает его
    декодированием utf-8-sig, а в /v1 файл приезжает строкой: BOM оставался
    приклеенным к первому заголовку («\\ufeffdate»), strip() его не снимает, и
    колонка даты не узнавалась."""
    client = await _linked_client(fresh_db, client_factory)
    preview = await client.post("/import/csv/preview", json={"csv": "﻿" + CSV_TWO_WORKOUTS})
    assert preview.status_code == 200, preview.text
    assert preview.json()["workout_count"] == 2
    assert preview.json()["set_count"] == 3

    resp = await client.post("/import/csv", json={"csv": "﻿" + CSV_TWO_WORKOUTS})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 2
    assert resp.json()["sets_imported"] == 3
