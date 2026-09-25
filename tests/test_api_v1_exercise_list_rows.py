"""Строка списка упражнений в приложении: миниатюра слева и «100×8 · вчера»
под именем — поля `thumb`, `has_photo` и `last_set` у каждого упражнения
GET /exercises (весь каталог, поиск, группа, архив) и у ответов на одно
упражнение (создание, правка, архив, форк шаблона), чтобы строка списка не
затиралась пустыми полями после правки.
"""

from __future__ import annotations

import base64

import httpx
import pytest

import analytics
import api_v1
import config
import progress_data

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


async def _workout(db, started_at: str, sets: dict[int, list[tuple]], status: str = "finished") -> int:
    """Тренировка с блоком на упражнение; подход — (weight, reps[, rpe[, load]])."""
    c = db.conn()
    cur = await c.execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
        (USER, started_at, started_at if status == "finished" else None, status),
    )
    wid = cur.lastrowid
    for order, (ex_id, rows) in enumerate(sets.items()):
        cur = await c.execute(
            "INSERT INTO workout_blocks (workout_id, order_index) VALUES (?, ?)", (wid, order)
        )
        bid = cur.lastrowid
        await c.execute(
            "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, 0)",
            (bid, ex_id),
        )
        for r, row in enumerate(rows):
            weight, reps, rpe, load = (tuple(row) + (None, None))[:4]
            await c.execute(
                "INSERT INTO sets (block_id, exercise_id, round_index, weight, reps, rpe, load_weight, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bid, ex_id, r, weight, reps, rpe, load, started_at),
            )
    await c.commit()
    return wid


def _by_id(payload) -> dict[int, dict]:
    return {e["id"]: e for e in payload}


async def test_field_shapes_catalog_and_custom(fresh_db):
    client = await _linked(fresh_db)
    catalog_id = await fresh_db.create_exercise(USER, CATALOG, None)
    own_id = await fresh_db.create_exercise(USER, "Моё странное движение", None)
    await _workout(fresh_db, "2026-09-20T10:00:00", {catalog_id: [(100, 8)]})

    listed = _by_id((await client.get("/exercises")).json())
    catalog, own = listed[catalog_id], listed[own_id]

    # Та же первая картинка, что у карточки упражнения.
    media = (await client.get(f"/exercises/{catalog_id}/media")).json()
    assert catalog["thumb"] == CATALOG_THUMB == media["images"][0]
    assert catalog["has_photo"] is False
    assert catalog["last_set"] == {"weight": 100, "reps": 8, "date": "2026-09-20"}
    assert isinstance(catalog["last_set"]["reps"], int)

    # Своё упражнение: каталожных кадров нет — null, истории нет — null.
    assert own["thumb"] is None
    assert own["has_photo"] is False
    assert own["last_set"] is None
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


async def test_last_set_takes_latest_finished_and_skips_active_and_backfill(fresh_db):
    client = await _linked(fresh_db)
    ex = await fresh_db.create_exercise(USER, CATALOG, None)
    await _workout(fresh_db, "2026-09-01T10:00:00", {ex: [(140, 5)]})  # старая, тяжелее
    await _workout(fresh_db, "2026-09-10T10:00:00", {ex: [(90, 10), (100, 8), (95, 8)]})
    # Позже по времени, но не законченные: открытая и недозанесённая задним числом.
    await _workout(fresh_db, "2026-09-12T10:00:00", {ex: [(200, 1)]}, status="active")
    await _workout(fresh_db, "2026-09-11T10:00:00", {ex: [(300, 1)]}, status="backfill")

    got = _by_id((await client.get("/exercises")).json())[ex]["last_set"]
    # e1RM: 90×10 → 120.0, 100×8 → 126.7 — лучший 100×8.
    assert got == {"weight": 100, "reps": 8, "date": "2026-09-10"}


async def test_last_set_matches_progress_top_set_rule(fresh_db):
    """Тот же выбор, что analytics.SessionStats.top_set (точка прогресса): RPE
    добавляет запас к повторам, e1RM — по нагрузке, у сессии своим весом —
    максимум повторов, формула атлета учитывается."""
    client = await _linked(fresh_db)
    await fresh_db.conn().execute("UPDATE users SET e1rm_formula = 'brzycki' WHERE telegram_id = ?", (USER,))
    await fresh_db.conn().commit()
    rpe_ex = await fresh_db.create_exercise(USER, "Жим с RPE", None)
    bw_ex = await fresh_db.create_exercise(USER, "Подтягивания своим весом", None)
    load_ex = await fresh_db.create_exercise(USER, "Отжимания с нагрузкой", None)
    await _workout(fresh_db, "2026-09-10T10:00:00", {
        rpe_ex: [(100, 9), (100, 8, 7.0)],       # 8 @7 = 11 до отказа > 9
        bw_ex: [(0, 12), (0, 15), (0, 10)],       # своим весом — по повторам
        load_ex: [(10, 10, None, 90), (30, 5, None, 110)],  # записан 10/30, нагрузка 90/110
    })

    listed = _by_id((await client.get("/exercises")).json())
    assert listed[rpe_ex]["last_set"]["reps"] == 8
    assert listed[bw_ex]["last_set"] == {"weight": 0, "reps": 15, "date": "2026-09-10"}
    # Отдаётся записанный вес, выбор — по нагрузке: 110×5 (128.3) > 90×10 (120).
    assert listed[load_ex]["last_set"]["weight"] == 30

    for ex_id in (rpe_ex, bw_ex, load_ex):
        top = (await progress_data.load_sessions(ex_id, "brzycki"))[-1].top_set
        assert isinstance(top, analytics.SetRow)
        assert listed[ex_id]["last_set"]["reps"] == top.reps


async def test_last_set_date_is_users_local_day(fresh_db):
    client = await _linked(fresh_db)
    await fresh_db.conn().execute("UPDATE users SET tz_offset = 3 WHERE telegram_id = ?", (USER,))
    await fresh_db.conn().commit()
    ex = await fresh_db.create_exercise(USER, CATALOG, None)
    await _workout(fresh_db, "2026-09-20T22:30:00", {ex: [(100, 8)]})  # UTC 22:30 → 01:30 по местному
    got = _by_id((await client.get("/exercises")).json())[ex]["last_set"]
    assert got["date"] == "2026-09-21"


async def test_fields_on_every_list_mode_and_single_responses(fresh_db):
    client = await _linked(fresh_db)
    groups = await fresh_db.list_muscle_groups(USER)
    gid = groups[0]["id"]
    ex = await fresh_db.create_exercise(USER, CATALOG, gid)
    arch = await fresh_db.create_exercise(USER, "Шраги со штангой", gid)
    await _workout(fresh_db, "2026-09-10T10:00:00", {ex: [(100, 8)], arch: [(60, 12)]})
    await client.post(f"/exercises/{arch}/archive")

    expected = {"weight": 100, "reps": 8, "date": "2026-09-10"}
    for url in ("/exercises", f"/exercises?group_id={gid}", "/exercises?query=жим"):
        row = _by_id((await client.get(url)).json())[ex]
        assert row["thumb"] == CATALOG_THUMB and row["last_set"] == expected, url
    archived = _by_id((await client.get("/exercises?archived=1")).json())[arch]
    assert archived["last_set"] == {"weight": 60, "reps": 12, "date": "2026-09-10"}
    assert archived["thumb"] == "/media/exercises/barbell_shrug_1.jpg"

    patched = (await client.patch(f"/exercises/{ex}", json={"description": "Медленно."})).json()
    assert patched["last_set"] == expected and patched["thumb"] == CATALOG_THUMB
    unarchived = (await client.post(f"/exercises/{arch}/unarchive")).json()
    assert unarchived["last_set"]["weight"] == 60
    created = (await client.post("/exercises", json={"name": "Совсем новое"})).json()
    assert created["thumb"] is None and created["last_set"] is None and created["has_photo"] is False


class _Counter:
    def __init__(self, db, monkeypatch):
        self.n = 0
        real = db.conn().execute

        def counting(*args, **kwargs):
            self.n += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(db.conn(), "execute", counting)

    async def measure(self, coro):
        before = self.n
        result = await coro
        return result, self.n - before


async def test_query_count_does_not_grow_with_the_list(fresh_db, monkeypatch):
    client = await _linked(fresh_db)

    async def add(n: int, start: int) -> None:
        ids = [await fresh_db.create_exercise(USER, f"Упражнение {start + i}", None) for i in range(n)]
        for k in range(3):
            await _workout(fresh_db, f"2026-09-{10 + k}T10:00:00", {e: [(50 + k, 8), (55 + k, 6)] for e in ids})

    await add(5, 0)
    counter = _Counter(fresh_db, monkeypatch)
    # Разогрев: первый запрос с токеном ещё пишет last_used_at — это не про список.
    await client.get("/exercises")
    resp, small = await counter.measure(client.get("/exercises"))
    assert len(resp.json()) == 5

    await add(195, 5)
    resp, large = await counter.measure(client.get("/exercises"))
    body = resp.json()
    assert len(body) == 200
    assert all(e["last_set"] == {"weight": 57, "reps": 6, "date": "2026-09-12"} for e in body)
    assert large == small, (small, large)
    # авторизация + строка пользователя + сам список + одна выборка last_set
    assert large <= 6, large


async def test_new_fields_do_not_depend_on_language(fresh_db):
    from tests.test_api_v1_language_invariant import language_violations

    results = {}
    for lang in ("ru", "en"):
        client = await _linked(fresh_db, lang)
        if lang == "ru":
            ex = await fresh_db.create_exercise(USER, CATALOG, None)
            await _workout(fresh_db, "2026-09-10T10:00:00", {ex: [(100, 8)]})
        body = (await client.get("/exercises")).json()
        row = _by_id(body)[ex]
        results[lang] = (row["thumb"], row["has_photo"], row["last_set"])
        # Новые поля не добавляют чужого языка ни в одну сторону.
        new_only = [{k: e[k] for k in ("thumb", "has_photo", "last_set")} for e in body]
        assert language_violations(new_only, lang) == []
    assert results["ru"] == results["en"]
