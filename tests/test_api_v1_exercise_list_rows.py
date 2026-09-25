"""Строка списка упражнений в приложении: миниатюра слева — поля `thumb` и
`has_photo` у каждого упражнения
GET /exercises (весь каталог, поиск, группа, архив) и у ответов на одно
упражнение (создание, правка, архив, форк шаблона), чтобы строка списка не
затиралась пустыми полями после правки.
"""

from __future__ import annotations

import base64

import httpx
import pytest

import api_v1
import config

pytestmark = pytest.mark.asyncio

USER = 111
CATALOG = "Жим штанги лёжа"  # в exercise_media.EXERCISE_IMAGE_SLUGS, оба кадра на диске
CATALOG_THUMB = "/media/exercises/barbell_bench_press_medium_grip_1.jpg"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked(fresh_db, lang: str = "ru") -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=USER, username="tester")
    await fresh_db.set_user_lang(USER, lang)
    code = await fresh_db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def _by_id(payload) -> dict[int, dict]:
    return {e["id"]: e for e in payload}


async def test_field_shapes_catalog_and_custom(fresh_db):
    client = await _linked(fresh_db)
    catalog_id = await fresh_db.create_exercise(USER, CATALOG, None)
    own_id = await fresh_db.create_exercise(USER, "Моё странное движение", None)

    listed = _by_id((await client.get("/exercises")).json())
    catalog, own = listed[catalog_id], listed[own_id]

    # Та же первая картинка, что у карточки упражнения.
    media = (await client.get(f"/exercises/{catalog_id}/media")).json()
    assert catalog["thumb"] == CATALOG_THUMB == media["images"][0]
    assert catalog["has_photo"] is False

    # Своё упражнение: каталожных кадров нет — null.
    assert own["thumb"] is None
    assert own["has_photo"] is False
    # Старые поля на месте.
    for key in ("display_name", "original_name", "has_description", "is_archived"):
        assert key in own


async def test_has_photo_flags_own_photo_without_leaking_it_into_thumb(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_PHOTO_DIR", str(tmp_path / "photos"))
    client = await _linked(fresh_db)
    catalog_id = await fresh_db.create_exercise(USER, CATALOG, None)
    own_id = await fresh_db.create_exercise(USER, "Моё странное движение", None)
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg").decode()
    for ex_id in (catalog_id, own_id):
        resp = await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": jpeg})
        assert resp.status_code == 201, resp.text

    listed = _by_id((await client.get("/exercises")).json())
    assert listed[catalog_id]["has_photo"] is True
    assert listed[catalog_id]["thumb"] == CATALOG_THUMB  # публичный кадр каталога, не своё фото
    assert listed[own_id]["has_photo"] is True
    assert listed[own_id]["thumb"] is None


async def test_fields_on_every_list_mode_and_single_responses(fresh_db):
    client = await _linked(fresh_db)
    groups = await fresh_db.list_muscle_groups(USER)
    gid = groups[0]["id"]
    ex = await fresh_db.create_exercise(USER, CATALOG, gid)
    arch = await fresh_db.create_exercise(USER, "Шраги со штангой", gid)
    await client.post(f"/exercises/{arch}/archive")

    for url in ("/exercises", f"/exercises?group_id={gid}", "/exercises?query=жим"):
        row = _by_id((await client.get(url)).json())[ex]
        assert row["thumb"] == CATALOG_THUMB and row["has_photo"] is False, url
    archived = _by_id((await client.get("/exercises?archived=1")).json())[arch]
    assert archived["thumb"] == "/media/exercises/barbell_shrug_1.jpg"

    patched = (await client.patch(f"/exercises/{ex}", json={"description": "Медленно."})).json()
    assert patched["thumb"] == CATALOG_THUMB
    unarchived = (await client.post(f"/exercises/{arch}/unarchive")).json()
    assert unarchived["thumb"] == "/media/exercises/barbell_shrug_1.jpg"
    created = (await client.post("/exercises", json={"name": "Совсем новое"})).json()
    assert created["thumb"] is None and created["has_photo"] is False


async def test_new_fields_do_not_depend_on_language(fresh_db):
    from tests.test_api_v1_language_invariant import language_violations

    results = {}
    for lang in ("ru", "en"):
        client = await _linked(fresh_db, lang)
        if lang == "ru":
            ex = await fresh_db.create_exercise(USER, CATALOG, None)
        body = (await client.get("/exercises")).json()
        row = _by_id(body)[ex]
        results[lang] = (row["thumb"], row["has_photo"])
        # Новые поля не добавляют чужого языка ни в одну сторону.
        new_only = [{k: e[k] for k in ("thumb", "has_photo")} for e in body]
        assert language_violations(new_only, lang) == []
    assert results["ru"] == results["en"]
