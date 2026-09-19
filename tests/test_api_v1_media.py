"""REST `/v1` для демонстраций упражнений: фото, клип, описание.

Маршруты api_v1_media.routes пока НЕ подключены в api_v1.routes (это делает
другой агент) — поэтому здесь собирается отдельное временное Starlette-
приложение из api_v1_media.routes напрямую, тем же способом, каким
api_v1.build_app собирает основное. Как только маршруты попадут в общий
список, этот файл можно (не обязательно) переключить на api_v1.build_app()
без единой другой правки в тестах ниже.
"""

import os

import httpx
import pytest
from starlette.applications import Starlette

import api_v1_common as common
import api_v1_media
import exercise_media

ApiError = common.ApiError


def _build_media_app() -> Starlette:
    return Starlette(
        routes=api_v1_media.routes,
        exception_handlers={
            ApiError: common.api_error_handler,
            Exception: common.unhandled_error_handler,
        },
    )


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=_build_media_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    """Тот же приём, что в tests/test_api_v1.py: /auth/link тут не смонтирован
    (это чужой маршрут из api_v1.routes), поэтому токен выпускается напрямую
    через db, минуя HTTP-эндпоинт связки."""
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    token = await fresh_db.issue_api_token(telegram_id)
    client = client_factory()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# 'Планка' — в EXERCISE_IMAGE_SLUGS (plank), с обоими фото на диске в
# media/exercises, но без демо-клипа (*_demo.mp4 не снят ни для одного
# упражнения на момент написания теста) — покрывает и наличие фото, и
# отсутствие клипа одним упражнением.
KNOWN_TEMPLATE = "Планка"
KNOWN_SLUG = "plank"


async def _create_exercise(fresh_db, user_id: int, name: str) -> int:
    return await fresh_db.create_exercise(user_id, name, group_id=None)


@pytest.mark.asyncio
async def test_media_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/exercises/1/media")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_media_for_exercise_with_known_template(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = await _create_exercise(fresh_db, 111, KNOWN_TEMPLATE)

    resp = await client.get(f"/exercises/{exercise_id}/media")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_media"] is True
    assert len(body["images"]) == 2
    assert all(u.startswith("/media/exercises/") for u in body["images"])
    assert body["images"][0].endswith(f"{KNOWN_SLUG}_1.jpg")
    assert body["images"][1].endswith(f"{KNOWN_SLUG}_2.jpg")
    # никакого клипа для этого упражнения ещё не снято
    assert body["animation"] is None


@pytest.mark.asyncio
async def test_media_for_exercise_without_catalog_entry(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = await _create_exercise(fresh_db, 111, "Моё придуманное упражнение")

    resp = await client.get(f"/exercises/{exercise_id}/media")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"images": [], "animation": None, "has_media": False}


@pytest.mark.asyncio
async def test_media_404_for_someone_elses_exercise(fresh_db, client_factory):
    other_exercise_id = await _create_exercise(fresh_db, 222, KNOWN_TEMPLATE)
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)

    resp = await client.get(f"/exercises/{other_exercise_id}/media")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_media_404_for_nonexistent_exercise(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/exercises/999999/media")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_description_present_for_known_template(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    # "Приседания без веса" имеет запись и в RU, и в EN словарях описаний.
    exercise_id = await _create_exercise(fresh_db, 111, "Приседания без веса")

    resp = await client.get(f"/exercises/{exercise_id}/description")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["description"], str)
    assert resp.json()["description"]


@pytest.mark.asyncio
async def test_description_404_when_absent(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = await _create_exercise(fresh_db, 111, "Совсем неизвестное упражнение")

    resp = await client.get(f"/exercises/{exercise_id}/description")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_description_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/exercises/1/description")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_description_404_for_someone_elses_exercise(fresh_db, client_factory):
    other_exercise_id = await _create_exercise(fresh_db, 222, "Приседания без веса")
    client = await _linked_client(fresh_db, client_factory, telegram_id=111)

    resp = await client.get(f"/exercises/{other_exercise_id}/description")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_serves_existing_media_file_with_correct_type(fresh_db, client_factory):
    # Статика без токена: см. комментарий в api_v1_media.get_media_file про
    # осознанный отказ от Bearer-токена для этого маршрута.
    client = client_factory()
    resp = await client.get(f"/media/exercises/{KNOWN_SLUG}_1.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert "cache-control" in resp.headers
    assert "max-age" in resp.headers["cache-control"]
    assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_serves_missing_media_file_as_404_not_500(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/media/exercises/does_not_exist_1.jpg")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_media_path_traversal_is_blocked(fresh_db, client_factory):
    """Самая важная проверка: имя из URL не должно уметь вылезти за
    exercise_media.MEDIA_DIR. Бьём напрямую по обработчику ASGI-транспортом,
    так что путь идёт как есть, тем же способом, каким подставной клиент мог
    бы собрать запрос."""
    client = client_factory()

    # Файл, который точно существует за пределами каталога с картинками —
    # если защита не сработает, тест это заметит по коду 200/содержимому.
    outside_file = os.path.join(os.path.dirname(os.path.dirname(exercise_media.MEDIA_DIR)), "db.py")
    assert os.path.isfile(outside_file)

    resp = await client.get("/media/exercises/../db.py")
    assert resp.status_code == 404

    resp2 = await client.get("/media/exercises/../../etc/passwd")
    assert resp2.status_code == 404

    # traversal, закодированный процентами — на случай, если бы кто-то решил
    # декодировать имя ещё раз внутри обработчика.
    resp3 = await client.get("/media/exercises/%2e%2e/%2e%2e/etc/passwd")
    assert resp3.status_code == 404


@pytest.mark.asyncio
async def test_media_traversal_cannot_escape_via_sibling_prefix(fresh_db, client_factory):
    """`/media/exercisesEVIL/...` не должно пройти проверку префикса просто
    потому что строка начинается с "media/exercises"."""
    client = client_factory()
    resp = await client.get("/media/exercises/../exercisesEVIL/whatever.jpg")
    assert resp.status_code == 404
