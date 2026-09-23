"""Бюджет запросов к SQLite у самых горячих ручек /v1 — и что их ответ
не поменялся ни на байт, пока запросы собирали пачками.

GET /workouts/{id} и GET /workouts/active раньше ходили в базу по разу на
каждый блок/упражнение (и дважды — у build_block_views и у самого JSON), а
«прошлая сессия» тянула всю историю упражнения и резала её по дате в Python:
78 и 36 запросов на аккаунте в 150 тренировок. Зал славы считал длительность
каждой тренировки отдельным запросом — 161. Приложение перезапрашивает
активную тренировку после каждого записанного подхода, так что это не
теоретика.

Две проверки на одном и том же сиде (150 тренировок × 6 упражнений, с
суперсетами, повтором упражнения в двух блоках, пустым блоком, RPE,
нагрузкой своим весом, заметками, занесёнными задним числом тренировками,
подходом, добавленным после финиша, и одинаковым started_at у двух
тренировок):

- ответ совпадает со снимком `snapshots/api_v1_query_budget.json`, снятым
  на коде ДО пакетной выборки — порядок, группировка и тексты те же;
- число `execute` на запрос не выше потолка.

Снимок обновляется только осознанно: `UPDATE_QUERY_SNAPSHOT=1 pytest
tests/test_api_v1_query_budget.py` — и дифф снимка тогда читается глазами
на ревью, потому что именно по нему видно, что поменялось для приложения.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import random
from pathlib import Path

import httpx
import pytest

import api_v1
import hall_of_fame_data
import view_builder

SNAPSHOT = Path(__file__).parent / "snapshots" / "api_v1_query_budget.json"
UPDATE = os.environ.get("UPDATE_QUERY_SNAPSHOT") == "1"

# Потолки — с небольшим запасом над замеренным; сюда входят и три запроса
# авторизации (токен, last_used_at, users). На этом сиде до пакетной выборки
# было ~78 / ~45 / 149, после — 13 / 12 / 11.
MAX_QUERIES_WORKOUT_DETAIL = 16
MAX_QUERIES_ACTIVE_WORKOUT = 14
MAX_QUERIES_HALL_OF_FAME = 13

FIXED_TODAY = dt.date(2026, 9, 1)


def _make_client():
    transport = httpx.ASGITransport(app=api_v1.build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _linked_client(fresh_db, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _make_client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _seed(db, user_id: int) -> dict:
    """Детерминированная история: одни и те же id и числа на любом коде."""
    rng = random.Random(20260923)
    c = db.conn()
    await c.execute("UPDATE users SET tz_offset = 3 WHERE telegram_id = ?", (user_id,))
    cur = await c.execute(
        "SELECT id FROM muscle_groups WHERE user_id IS NULL ORDER BY id LIMIT 4"
    )
    groups = [r["id"] for r in await cur.fetchall()]
    cur = await c.execute(
        "INSERT INTO muscle_groups (user_id, name, is_archived) VALUES (?, 'Своя', 1)", (user_id,)
    )
    own_group = cur.lastrowid
    # (имя, группа, bodyweight_load): свой вес, без группы и со своей
    # архивной группой — ветки «без группы»/None в тегах и в карточке.
    specs = [
        ("Жим лёжа", groups[0], "none"),
        ("Присед", groups[1], "none"),
        ("Тяга", groups[2], "none"),
        ("Жим стоя", groups[3], "none"),
        ("Подтягивания", groups[2], "full"),
        ("Отжимания", groups[0], "full"),
        ("Скручивания", None, "none"),
        ("Странное", own_group, "none"),
    ]
    ex_ids = []
    for name, gid, bw in specs:
        cur = await c.execute(
            "INSERT INTO exercises (user_id, name, primary_group_id, display_name, "
            "original_name, created_at, bodyweight_load) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, gid, name, name, "2025-12-01T00:00:00", bw),
        )
        ex_ids.append(cur.lastrowid)

    base = dt.datetime(2026, 1, 1, 6, 0, 0)
    workouts = []
    start = base
    for i in range(150):
        # Поздний вечер/раннее утро — чтобы tz_offset двигал местный день.
        start = start + dt.timedelta(days=rng.choice([1, 1, 2, 3]), hours=rng.choice([-5, 0, 3, 7]))
        started_at = start.isoformat(timespec="seconds")
        if i == 77:
            started_at = workouts[-1]["started_at"]  # одинаковый started_at
        backfill = i % 13 == 5
        wid = await _insert_workout(c, rng, user_id, ex_ids, started_at, i, backfill, "finished")
        workouts.append({"id": wid, "started_at": started_at, "i": i})

    active_start = (start + dt.timedelta(days=2)).isoformat(timespec="seconds")
    active_id = await _insert_workout(c, rng, user_id, ex_ids, active_start, 150, False, "active")
    await c.commit()
    by_i = {w["i"]: w["id"] for w in workouts}
    return {
        "detail_ids": [by_i[k] for k in (0, 2, 4, 5, 8, 13, 76, 77, 78, 149)] + [active_id],
        "active_id": active_id,
        "ex_ids": ex_ids,
    }


async def _insert_workout(c, rng, user_id, ex_ids, started_at, i, backfill, status) -> int:
    start = dt.datetime.fromisoformat(started_at)
    finished_at = started_at if backfill else (start + dt.timedelta(minutes=70)).isoformat(timespec="seconds")
    if status != "finished":
        finished_at = None
    cur = await c.execute(
        "INSERT INTO workouts (user_id, started_at, finished_at, status, note) VALUES (?, ?, ?, ?, ?)",
        (user_id, started_at, finished_at, status, f"note {i}" if i % 7 == 0 else None),
    )
    wid = cur.lastrowid
    chosen = rng.sample(ex_ids, 6)
    if i % 9 == 2:
        chosen.append(chosen[0])  # то же упражнение вторым блоком — слияние
    minute = 0
    for order, ex_id in enumerate(chosen):
        superset = (i % 10 == 3 or i == 150) and order == 1
        cur = await c.execute(
            "INSERT INTO workout_blocks (workout_id, order_index, type) VALUES (?, ?, ?)",
            (wid, order, "superset" if superset else "single"),
        )
        bid = cur.lastrowid
        members = [ex_id] + ([chosen[2]] if superset else [])
        for k, m in enumerate(members):
            await c.execute(
                "INSERT INTO block_exercises (block_id, exercise_id, order_in_block) VALUES (?, ?, ?)",
                (bid, m, k),
            )
        if i % 11 == 4 and order == 5:
            continue  # пустой блок: упражнение добавили и бросили
        n_sets = rng.choice([2, 3, 3, 4])
        for r in range(n_sets):
            for k, m in enumerate(members):
                bw = ex_ids.index(m) in (4, 5)
                weight = 0.0 if bw and rng.random() < 0.6 else float(rng.choice([20, 40, 60, 80, 100, 102.5]) + (i // 10 if i < 150 else 40))
                reps = rng.choice([1, 3, 5, 8, 10, 12, 15])
                rpe = rng.choice([None, None, 8.0, 9.5, 10.0])
                load = (weight + 80.0) if bw else rng.choice([None, None, weight])
                minute += rng.choice([2, 3, 4])
                created = start + dt.timedelta(minutes=minute)
                if i % 17 == 8 and order == 5 and r == n_sets - 1:
                    created = start + dt.timedelta(hours=5)  # правка после финиша
                await c.execute(
                    "INSERT INTO sets (block_id, exercise_id, round_index, order_in_round, weight, reps, "
                    "rpe, load_weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (bid, m, r, k, weight, reps, rpe, load, created.isoformat(timespec="seconds")),
                )
        if i % 6 == 1 or i == 150:
            await c.execute(
                "INSERT OR REPLACE INTO exercise_notes (workout_id, exercise_id, note) VALUES (?, ?, ?)",
                (wid, ex_id, f"заметка {i}/{order}"),
            )
    return wid


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


def _views_json(views) -> list:
    return [dataclasses.asdict(v) for v in views]


@pytest.mark.asyncio
async def test_hot_endpoints_match_snapshot_within_query_budget(fresh_db, monkeypatch):
    monkeypatch.setattr(hall_of_fame_data.timeutil, "user_today", lambda _user: FIXED_TODAY)
    client = await _linked_client(fresh_db)
    # Соседний атлет с похожей историей — его подходы не должны протекать в
    # «прошлую»/рекорды/🥇 первого.
    await fresh_db.get_or_create_user(telegram_id=222, username="other")
    await _seed(fresh_db, 222)
    seeded = await _seed(fresh_db, 111)
    counter = _Counter(fresh_db, monkeypatch)

    actual: dict = {"detail": {}, "views": {}}
    counts: dict = {"detail": {}}
    for wid in seeded["detail_ids"]:
        resp, n = await counter.measure(client.get(f"/workouts/{wid}"))
        assert resp.status_code == 200, resp.text
        actual["detail"][str(wid)] = resp.json()
        counts["detail"][str(wid)] = n

    resp, counts["active"] = await counter.measure(client.get("/workouts/active"))
    assert resp.status_code == 200, resp.text
    actual["active"] = resp.json()

    resp, counts["hall_of_fame"] = await counter.measure(client.get("/hall-of-fame"))
    assert resp.status_code == 200, resp.text
    actual["hall_of_fame"] = resp.json()

    # Тот же сборщик зовёт и бот (карточка, живой трекер, история) — его вид
    # сверяется тоже, со всеми сочетаниями флагов, что встречаются в handlers.
    for wid in seeded["detail_ids"]:
        workout = await fresh_db.get_workout(wid)
        for name, kwargs in {
            "plain": {},
            "golds": {"mark_golds": True},
            "prev_only": {"previous_before": workout["started_at"]},
            "prev_records": {"previous_before": workout["started_at"], "mark_records": True},
            "records_only": {"mark_records": True},
        }.items():
            views = await view_builder.build_block_views(wid, "brzycki", **kwargs)
            actual["views"][f"{wid}:{name}"] = _views_json(views)
    actual["pick"] = {
        str(wid): await view_builder.workout_pick_exercises(wid) for wid in seeded["detail_ids"]
    }
    actual["longest"] = await view_builder.longest_workout_seconds(111)
    actual = json.loads(json.dumps(actual, default=str, ensure_ascii=False))

    print("query counts:", json.dumps(counts))
    if UPDATE:
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        pytest.skip("snapshot updated")

    expected = json.loads(SNAPSHOT.read_text())
    for section in expected:
        if isinstance(expected[section], dict):
            for key in expected[section]:
                assert actual[section][key] == expected[section][key], f"{section}/{key} differs"
    assert actual == expected

    for wid, n in counts["detail"].items():
        assert n <= MAX_QUERIES_WORKOUT_DETAIL, f"GET /workouts/{wid}: {n} queries"
    assert counts["active"] <= MAX_QUERIES_ACTIVE_WORKOUT, counts
    assert counts["hall_of_fame"] <= MAX_QUERIES_HALL_OF_FAME, counts
