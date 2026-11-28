"""«Место в зале» (crowd_standing.py) и слайд тоннажа в итогах
(finish_tonnage.py): математика корзин, порог, пол, правила процента,
одна корзина на все языки, своё и объединённое упражнение, свои аккаунты,
ручки /v1 и ключи `rewards.tonnage_object` / `rewards.standing` у finish."""

from __future__ import annotations

import re

import httpx
import pytest
from standing_crowd import athlete, crowd

import api_v1
import config
import crowd_standing
import finish_tonnage
import i18n
import lifecycle_events
import seed_data

BENCH = "Жим штанги лёжа"
SQUAT = "Присед со штангой"
PULLUPS = "Подтягивания"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")
ME = 111


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _me(db, lang: str = "ru", gender=None, uid: int = ME) -> httpx.AsyncClient:
    await db.get_or_create_user(telegram_id=uid, username=f"u{uid}")
    await db.set_user_lang(uid, lang)
    if gender:
        await db.set_user_gender(uid, gender)
    client = _client()
    client.headers["Authorization"] = f"Bearer {await db.issue_api_token(uid)}"
    return client


def _spread(n: int, lo: float = 60, hi: float = 140) -> list[float]:
    return [round(lo + (hi - lo) * i / (n - 1), 1) for i in range(n)]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


# ---------- математика ----------


def test_k_is_config_and_matches_site():
    import site_crowd

    assert config.STANDING_MIN_ATHLETES == site_crowd.K_MIN_USERS == 20


def test_bucket_below_k_has_no_placement():
    k = config.STANDING_MIN_ATHLETES
    small = crowd_standing.build_bucket(list(range(k - 1)))
    assert "q" not in small and crowd_standing.place(small, 5) is None
    assert crowd_standing.place(crowd_standing.build_bucket(list(range(k))), 5) is not None


def test_top_is_rounded_up_to_five_and_capped_by_size():
    bucket = crowd_standing.build_bucket([float(v) for v in range(1, 101)])
    p = crowd_standing.place(bucket, 70.0)
    # ~69.7% ниже → «топ-30.3%» → вверх до 35: проценты не приукрашены.
    assert p.above_median and p.top == 35 and p.next_top == 30 and p.to_next > 0
    best = crowd_standing.place(bucket, 100.0)
    assert best.top == 5 and best.next_top is None  # меньше 200 — потолок топ-5%
    big = crowd_standing.build_bucket([float(v) for v in range(1, 251)])
    assert crowd_standing.place(big, 250.0).top == 1
    assert crowd_standing.place(big, 240.0).top == 5
    assert crowd_standing.place(big, 240.0).next_top == 1


def test_strongest_is_number_one_not_top_five():
    """Лучшему — «№1 в дневнике», а не «топ-5%» (читалось как «впереди ещё 5%»)."""
    bucket = crowd_standing.build_bucket([float(v) for v in range(1, 101)])
    assert crowd_standing.place(bucket, 100.0).best
    assert crowd_standing.place(bucket, 130.0).best  # живой рекорд сильнее вчерашнего максимума
    assert not crowd_standing.place(bucket, 99.0).best
    with i18n.use_lang("ru"):
        assert i18n.t("standing.label.best") == "№1 в дневнике"


def test_below_median_has_no_percent():
    bucket = crowd_standing.build_bucket([float(v) for v in range(1, 101)])
    p = crowd_standing.place(bucket, 30.0)
    assert not p.above_median and p.top is None and p.to_median == pytest.approx(50.5 - 30.0)


def test_gender_bucket_falls_back_to_all():
    k = config.STANDING_MIN_ATHLETES
    rows = [(i, BENCH, 100.0 + i, None, 5, "male", None) for i in range(k)]
    rows += [(1000 + i, BENCH, 50.0 + i, None, 5, "female", None) for i in range(3)]
    entry = crowd_standing.aggregate(rows)[BENCH]
    assert crowd_standing.pick_bucket(entry, "male", "abs")[0] == "male"
    assert crowd_standing.pick_bucket(entry, "female", "abs")[0] == "all"
    assert crowd_standing.pick_bucket(entry, None, "abs")[0] == "all"


def test_custom_names_and_timed_are_not_bucketed():
    k = config.STANDING_MIN_ATHLETES
    rows = [(i, "Мой жим", 100.0, None, 5, None, None) for i in range(k)]
    rows += [(i, "Планка", None, None, 60, None, None) for i in range(k)]
    # Своё упражнение, названное как шаблон (другой регистр и «е» вместо «ё»), — та же идентичность.
    rows += [(i, "жим штанги лежа", 100.0 + i, None, 5, None, None) for i in range(k)]
    out = crowd_standing.aggregate(rows)
    assert set(out) == {BENCH}


def test_tonnage_milestones():
    assert finish_tonnage.crossed_lifetime(29000, 31000)
    assert not finish_tonnage.crossed_lifetime(42000, 140000)
    assert finish_tonnage.crossed_lifetime(35000, 42000)  # пассажирский самолёт, 41т
    assert finish_tonnage.crossed_lifetime(410000, 421000)  # космическая станция, 420т
    assert not finish_tonnage.crossed_lifetime(421000, 800000)  # до двух станций ещё далеко
    assert finish_tonnage.crossed_lifetime(830000, 845000)  # каждые +420т после станции
    assert finish_tonnage.crossed_lifetime(1250000, 1270000)  # три станции
    assert finish_tonnage.next_target(500000) == ("space_station", 2)
    assert finish_tonnage.workout_object(100, 1) is None
    obj, count = finish_tonnage.workout_object(5000, 7)
    assert 1 <= count <= finish_tonnage.MAX_COUNT and 5000 // dict(finish_tonnage._objects())[obj] == count
    # Соседние номера тренировок — соседние предметы, не один и тот же.
    picks = [finish_tonnage.workout_object(5000, n)[0] for n in range(6)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))


def test_new_objects_only_for_builds_that_draw_them():
    assert finish_tonnage.client_build("1.0.9 (139)") == 139
    assert finish_tonnage.client_build("1.0.9") is None and finish_tonnage.client_build(None) is None
    every = {obj for obj, _w in finish_tonnage._objects()}
    assert every >= finish_tonnage.NEW_IDS
    with lifecycle_events.using("ios", "1.0.9 (138)"):
        old = finish_tonnage.drawable_ids()
    with lifecycle_events.using("ios", f"1.0.9 ({finish_tonnage.NEW_IDS_MIN_BUILD})"):
        assert finish_tonnage.drawable_ids() == every
    assert not old & finish_tonnage.NEW_IDS
    for n in range(40):
        obj, _c = finish_tonnage.workout_object(5000, n, old)
        assert obj not in finish_tonnage.NEW_IDS


def test_every_object_id_has_a_name_in_every_language():
    ids = {obj for obj, _w in finish_tonnage._objects()}
    assert not ids & {"tank", "statue_of_liberty"}  # предметы вне политики
    assert {"trex", "humpback", "airliner", "blue_whale", "space_station", "saint_bernard", "bus"} <= ids
    for lang in LANGS:
        for obj in ids:
            for n in (1, 2, 5):
                text = i18n.t_in(lang, f"tonnage.object.{obj}", n=n)
                assert text and not text.startswith("tonnage.")


# ---------- ручки ----------


async def test_ru_and_en_athletes_share_one_bucket(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    half = k // 2
    await crowd(fresh_db, BENCH, _spread(half), lang="ru", start=50000)
    # Англоязычные атлеты: показанное имя — английское, идентичность — та же.
    for i, w in enumerate(_spread(k - half, 65, 135)):
        await athlete(fresh_db, 60000 + i, BENCH, w, lang="en", name="Barbell Bench Press")
    ru = await _me(fresh_db, "ru")
    ex = await athlete(fresh_db, ME, BENCH, 120)
    body = (await ru.get(f"/exercises/{ex}/standing")).json()
    assert body["available"] and body["above_median"] and body["top"] and body["label"].startswith("топ-")
    assert len(body["histogram"]) == 20 and 0 <= body["you_bin"] < 20
    en = await _me(fresh_db, "en", uid=222)
    ex_en = await athlete(fresh_db, 222, BENCH, 120, name="Barbell Bench Press", lang="en")
    body_en = (await en.get(f"/exercises/{ex_en}/standing")).json()
    assert body_en["available"] and body_en["top"] == body["top"]
    assert not [s for s in _strings(body_en) if CYRILLIC.search(s)]
    assert "athletes" not in body and "athletes" not in body_en


async def test_below_median_label_and_threshold(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k - 2))
    client = await _me(fresh_db)
    ex = await athlete(fresh_db, ME, BENCH, 50)
    few = (await client.get(f"/exercises/{ex}/standing")).json()
    # Меня самого в корзине k-1: ещё мало.
    assert not few["available"] and few["reason"] == "few_athletes" and few["histogram"] == []
    await crowd(fresh_db, BENCH, [100.0], start=59000)
    await crowd_standing.refresh(force=True)
    body = (await client.get(f"/exercises/{ex}/standing")).json()
    assert body["available"] and not body["above_median"] and body["top"] is None
    assert body["stronger_than"] is None and "%" not in body["label"]
    assert body["label"].startswith("до середины +") and body["label"].endswith("кг")
    assert body["hint"]


async def test_reasons_and_404(fresh_db):
    client = await _me(fresh_db)
    own = await athlete(fresh_db, ME, None, 50, name="Мой жим")
    body = (await client.get(f"/exercises/{own}/standing")).json()
    assert body["reason"] == "not_catalog" and set(body) >= {
        "available", "reason", "exercise_id", "name", "metric", "basis", "above_median", "top", "stronger_than",
        "label", "value_text", "cohort_text", "hint", "histogram", "you_bin", "axis_min", "axis_median", "axis_max"}
    await athlete(fresh_db, 333, BENCH, 50)
    assert (await client.get("/exercises/999999/standing")).status_code == 404
    old = await athlete(fresh_db, ME, SQUAT, 100, days_ago=120)
    assert (await client.get(f"/exercises/{old}/standing")).json()["reason"] == "no_data"
    assert (await client.get(f"/exercises/{old}/standing?basis=nope")).status_code == 400


async def test_merged_custom_exercise_counts_and_custom_does_not(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k - 1))
    # Свой «мой жим» без каталога — не в корзине.
    custom = await athlete(fresh_db, 70000, None, 100, name="мой жим")
    await crowd_standing.refresh(force=True)
    assert crowd_standing.cached()[BENCH]["cohorts"]["all"]["abs"]["n"] == k - 1
    # Объединил со своей копией каталога — подходы переехали, атлет в корзине.
    c = fresh_db.conn()
    cur = await c.execute(
        "INSERT INTO exercises (user_id, name, display_name, original_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (70000, BENCH, BENCH, BENCH, "2026-01-01T00:00:00"))
    fork = cur.lastrowid
    await c.commit()
    assert await fresh_db.merge_exercises(70000, fork, custom) == fresh_db.MERGE_OK
    await crowd_standing.refresh(force=True)
    assert crowd_standing.cached()[BENCH]["cohorts"]["all"]["abs"]["n"] == k


async def test_own_accounts_are_excluded(fresh_db, monkeypatch):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k), start=50000)
    monkeypatch.setattr(config, "ADMIN_ID", 50000)
    await crowd_standing.refresh(force=True)
    assert crowd_standing.cached()[BENCH]["cohorts"]["all"]["abs"]["n"] == k - 1


async def test_gender_cohort_text_and_rel_basis(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k), gender="male", bodyweight=80)
    client = await _me(fresh_db, gender="male")
    ex = await athlete(fresh_db, ME, BENCH, 120, bodyweight=90)
    abs_ = (await client.get(f"/exercises/{ex}/standing")).json()
    assert abs_["cohort_text"] == "мужчины" and abs_["basis"] == "abs"
    rel = (await client.get(f"/exercises/{ex}/standing?basis=rel")).json()
    assert rel["basis"] == "rel" and rel["rel_available"] and "веса тела" in rel["value_text"]
    pull = await athlete(fresh_db, ME, PULLUPS, 0, 12)
    reps = (await client.get(f"/exercises/{pull}/standing?basis=rel")).json()
    assert reps["metric"] == "reps" and reps["basis"] == "abs"


async def test_overview_orders_available_first(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k + 5), start=50000)
    await crowd(fresh_db, SQUAT, _spread(k, 80, 180), start=52000)
    client = await _me(fresh_db)
    await athlete(fresh_db, ME, BENCH, 100)
    await athlete(fresh_db, ME, SQUAT, 150)
    await athlete(fresh_db, ME, "Становая тяга", 150)
    await athlete(fresh_db, ME, None, 10, name="Мой жим")
    body = (await client.get("/standing")).json()
    assert body["basis"] == "abs" and body["intro"]
    names = [(i["name"], i["available"]) for i in body["items"]]
    assert names == [(BENCH, True), (SQUAT, True), ("Становая тяга", False)]


async def test_no_string_names_the_bucket_size(fresh_db):
    """Сколько атлетов в корзине, не говорит ни одна строка — ни числом, ни ступенью."""
    size = 47
    await crowd(fresh_db, BENCH, [60.0 + 2 * i for i in range(size - 1)])
    client = await _me(fresh_db)
    ex_id = await athlete(fresh_db, ME, BENCH, 80, days_ago=20)
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    await crowd_standing.refresh(force=True)
    assert crowd_standing.cached()[BENCH]["cohorts"]["all"]["abs"]["n"] == size
    r = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": 140, "reps": 3})
    assert r.status_code == 201, r.text
    finish = (await client.post(f"/workouts/{wid}/finish", json={})).json()
    standing = finish["rewards"]["standing"]
    assert standing is not None
    exercise = (await client.get(f"/exercises/{ex_id}/standing")).json()
    overview = (await client.get("/standing")).json()
    pattern = re.compile(rf"(?<!\d){size}(?!\d)")
    leaks = [s for s in _strings([standing, exercise, overview]) if pattern.search(s)]
    assert not leaks
    assert "athletes" not in standing


# ---------- итог тренировки ----------


async def _finish(client, db, ex_id: int, weight: float, reps: int = 5) -> dict:
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    r = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": ex_id, "weight": weight, "reps": reps})
    assert r.status_code == 201, r.text
    resp = await client.post(f"/workouts/{wid}/finish", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("lang", LANGS)
async def test_standing_in_rewards_only_on_record_above_median(fresh_db, lang):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k * 2))
    client = await _me(fresh_db, lang)
    ex = await athlete(fresh_db, ME, BENCH, 90, days_ago=10, name=seed_data.localized_exercise_name(BENCH, lang))
    await crowd_standing.refresh(force=True)
    # Ниже прошлого — не рекорд.
    assert (await _finish(client, fresh_db, ex, 80))["rewards"]["standing"] is None
    rewards = (await _finish(client, fresh_db, ex, 120, 3))["rewards"]
    st = rewards["standing"]
    assert st is not None and st["exercise_id"] == ex and 5 <= st["top"] <= 50
    assert st["stronger_than"] == 100 - st["top"] and 0 <= st["crowd_index"] <= 19
    assert st["gained"] is None or st["gained"] > 0
    assert set(st) >= {"name", "eyebrow", "headline", "hint", "footnote"}
    obj = rewards["tonnage_object"]
    assert obj["period"] == "workout" and obj["count"] >= 1
    if lang != "ru":
        leaks = [s for s in _strings(rewards) if CYRILLIC.search(s)]
        assert not leaks
    else:
        assert st["eyebrow"].startswith("НОВЫЙ РЕКОРД · ")


async def test_no_standing_below_median_or_without_history(fresh_db):
    k = config.STANDING_MIN_ATHLETES
    await crowd(fresh_db, BENCH, _spread(k * 2, 100, 200))
    client = await _me(fresh_db)
    ex = await athlete(fresh_db, ME, BENCH, 50, days_ago=10)
    await crowd_standing.refresh(force=True)
    # Рекорд, но ниже середины — места нет.
    assert (await _finish(client, fresh_db, ex, 60))["rewards"]["standing"] is None
    fresh = await athlete(fresh_db, ME, SQUAT, 0, days_ago=500)  # пустая история приседа
    await crowd(fresh_db, SQUAT, _spread(k), start=80000)
    await crowd_standing.refresh(force=True)
    assert (await _finish(client, fresh_db, fresh, 300))["rewards"]["standing"] is None


async def _past_workout(db, tonnage_kg: float, *, imported: bool = False, days_ago: float = 40) -> None:
    """Прошлая закрытая тренировка атлета ME с заданным тоннажем (1 подход)."""
    ex = await athlete(db, ME, SQUAT, tonnage_kg / 10, 10, days_ago=days_ago)
    if imported:
        await db.conn().execute(
            "UPDATE workouts SET import_batch_id = 'b1' WHERE id = (SELECT b.workout_id FROM workout_blocks b "
            "JOIN sets s ON s.block_id = b.id WHERE s.exercise_id = ?)", (ex,))
        await db.conn().commit()


async def test_tonnage_lifetime_milestone_wins(fresh_db):
    client = await _me(fresh_db)
    await _past_workout(fresh_db, 29000)
    ex = await athlete(fresh_db, ME, BENCH, 10, days_ago=60)
    obj = (await _finish(client, fresh_db, ex, 200, 10))["rewards"]["tonnage_object"]
    assert obj["period"] == "lifetime" and obj["id"] == "humpback" and obj["count"] == 1
    assert obj["eyebrow"] == "ЗА ВСЁ ВРЕМЯ ТЫ ПОДНЯЛ" and obj["scale_text"] == "1 КИТ-ГОРБАЧ = 30Т"
    assert obj["next_title"] == "Следующая цель — пассажирский самолёт, 41т"
    assert obj["caption"].startswith("3 тренировки")


async def test_imported_history_celebrates_nothing(fresh_db):
    client = await _me(fresh_db)
    await _past_workout(fresh_db, 29000, imported=True)
    ex = await athlete(fresh_db, ME, BENCH, 10, days_ago=60)
    obj = (await _finish(client, fresh_db, ex, 200, 10))["rewards"]["tonnage_object"]
    assert obj["period"] == "workout"


async def test_workout_object_rotates_and_never_month(fresh_db):
    client = await _me(fresh_db)
    ex = await athlete(fresh_db, ME, BENCH, 10, days_ago=60)
    first = (await _finish(client, fresh_db, ex, 130, 10))["rewards"]["tonnage_object"]
    second = await _finish(client, fresh_db, ex, 130, 10)
    obj = second["rewards"]["tonnage_object"]
    # Тот же тоннаж, месячный рос — а слайд всё равно про тренировку, и предмет другой.
    assert first["period"] == obj["period"] == "workout" and first["id"] != obj["id"]
    replay = await client.post(f"/workouts/{second['id']}/finish", json={})
    assert replay.json()["rewards"]["tonnage_object"] == obj


async def test_backfill_shows_only_the_workout(fresh_db):
    client = await _me(fresh_db)
    await _past_workout(fresh_db, 29000)
    ex = await athlete(fresh_db, ME, BENCH, 10, days_ago=60)
    bf = (await client.post("/workouts/backfill", json={"date": "2026-01-10"})).json()
    await client.post(f"/workouts/{bf['id']}/sets", json={"exercise_id": ex, "weight": 200, "reps": 10})
    obj = (await client.post(f"/workouts/{bf['id']}/finish", json={})).json()["rewards"]["tonnage_object"]
    assert obj["period"] == "workout"
