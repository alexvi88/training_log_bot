"""«Неделя комиксом» (weekly_comic, поле `comic` в GET /v1/weekly-summary):
какие четыре кадра и с какими подписями.

Сбор — через сам /v1, как у test_weekly_summary: неделя — прошлая законченная
от настоящего «сегодня» атлета (tz_offset = 0, если не сказано иначе).
Правила подписей на всех языках — чистой функцией build.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx
import pytest

import api_v1
import i18n
import timeutil
import weekly_comic
import weekly_summary
from weekly_comic import ComicFacts, DayStory, SetPick

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
ALL_LANGS = ("ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr")


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(fresh_db, client_factory, lang="ru", unit="kg", tz=0):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    await fresh_db.update_user(111, tz_offset=tz, unit=unit)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _week(db) -> dt.date:
    return weekly_summary.default_week(timeutil.user_today(await db.get_user(111)))


async def _exercise(db, name: str, group: str = "Грудь") -> int:
    groups = {g["name"]: g["id"] for g in await db.list_muscle_groups(None, global_only=True)}
    return await db.create_exercise(111, name, groups[group])


async def _train(db, day: dt.date, ex_id: int, weight=60.0, reps=5, sets=3, hour=10, tz=0) -> int:
    """Тренировка, начатая в `hour` по часам атлета (в базе — UTC)."""
    start = dt.datetime.combine(day, dt.time(hour)) - dt.timedelta(hours=tz)
    workout_id = await db.create_finished_workout(
        111, start.isoformat(), (start + dt.timedelta(hours=1)).isoformat()
    )
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    for _ in range(sets):
        await db.append_set(block_id, ex_id, 0, weight, reps)
    return workout_id


async def _usual_three(db, week: dt.date, ex: int, weeks: int = 4) -> None:
    """Четыре недели по три тренировки до целевой — «обычно» 3."""
    for w in range(1, weeks + 1):
        for d in (0, 2, 4):
            await _train(db, week - dt.timedelta(weeks=w) + dt.timedelta(days=d), ex)


async def _comic(client, week: dt.date):
    body = (await client.get(f"/weekly-summary?week={week.isoformat()}")).json()
    return body["comic"]


def _check_shape(comic):
    assert len(comic["panels"]) == 4
    for p in comic["panels"]:
        assert set(p) == {"day", "label", "scene", "caption", "record"}
        assert p["scene"] in weekly_comic.SCENES
        assert p["label"] == p["label"].upper()
        assert p["caption"] and "{" not in p["caption"] and "#" not in p["caption"]
    assert comic["panels"][3]["scene"] in ("week_closed", "week_partial")
    assert comic["panels"][3]["day"] is None


# ---------- сбор через /v1 ----------

async def test_empty_week_has_no_comic(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    await _train(fresh_db, week - dt.timedelta(days=3), ex)
    assert await _comic(client, week) is None


async def test_record_week_closed_by_the_norm(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    bench = await _exercise(fresh_db, "Жим лёжа")
    squat = await _exercise(fresh_db, "Присед", "Ноги")
    row = await _exercise(fresh_db, "Тяга в наклоне", "Спина")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, bench)
    await _train(fresh_db, week, squat, weight=100)
    await _train(fresh_db, week + dt.timedelta(days=2), bench, weight=80, sets=1)
    await _train(fresh_db, week + dt.timedelta(days=4), row, weight=70)

    comic = await _comic(client, week)
    _check_shape(comic)
    mon, wed, fri, total = comic["panels"]
    assert (mon["day"], mon["label"], mon["scene"], mon["record"]) == (week.isoformat(), "ПН", "squat", False)
    assert mon["caption"].startswith("Присед 100кг×5.")
    assert (wed["label"], wed["scene"], wed["record"]) == ("СР", "bench", True)
    assert wed["caption"].startswith("Жим лёжа 80кг×5 — рекорд.")
    assert (fri["label"], fri["scene"]) == ("ПТ", "pull")
    assert total["label"] == "ИТОГ" and total["scene"] == "week_closed"
    assert total["caption"].startswith("3 из 3.")


async def test_english_labels_and_record_caption(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="en")
    bench = await _exercise(fresh_db, "Bench press")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, bench)
    for d, weight in ((0, 60), (2, 60), (4, 82.5)):
        await _train(fresh_db, week + dt.timedelta(days=d), bench, weight=weight)
    comic = await _comic(client, week)
    _check_shape(comic)
    assert [p["label"] for p in comic["panels"]] == ["MON", "WED", "FRI", "TOTAL"]
    assert comic["panels"][2]["caption"].startswith("Bench press 82.5kg×5 — a record.")
    assert comic["panels"][3]["caption"].startswith("3 out of 3.")
    assert not any(CYRILLIC.search(p["caption"] + p["label"]) for p in comic["panels"])


async def test_night_and_morning_by_local_clock(fresh_db, client_factory):
    # UTC+3: 20:00 UTC — 23:00 у атлета, 03:00 UTC — 06:00 у атлета.
    client = await _client(fresh_db, client_factory, tz=3)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, ex)
    await _train(fresh_db, week, ex, hour=23, tz=3)
    await _train(fresh_db, week + dt.timedelta(days=2), ex, hour=6, tz=3)
    await _train(fresh_db, week + dt.timedelta(days=4), ex, hour=7, tz=3)
    await _train(fresh_db, week + dt.timedelta(days=5), ex, hour=4, tz=3, sets=1)
    comic = await _comic(client, week)
    _check_shape(comic)
    # Четыре дня: три особых (ночь, утро, ночь в 04:00) важнее обычного в 07:00.
    scenes = [(p["label"], p["scene"]) for p in comic["panels"][:3]]
    assert scenes == [("ПН", "night"), ("СР", "morning"), ("СБ", "night")]
    assert "Жим лёжа 60кг×5" in comic["panels"][0]["caption"]


async def test_comeback_is_support_without_a_skip(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    # «Обычно» 3, потом три недели тишины, потом одна тренировка.
    await _usual_three(fresh_db, week - dt.timedelta(weeks=3), ex)
    await _train(fresh_db, week + dt.timedelta(days=1), ex)
    comic = await _comic(client, week)
    _check_shape(comic)
    scenes = [p["scene"] for p in comic["panels"]]
    assert "comeback" in scenes and "skip" not in scenes
    comeback = next(p for p in comic["panels"] if p["scene"] == "comeback")
    assert comeback["label"] == "ВТ" and "3 недел" in comeback["caption"]
    # Норма не взята — неделя частичная, но подколки нет.
    assert comic["panels"][3]["scene"] == "week_partial"


async def test_first_week_starts_with_first(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    await _train(fresh_db, week + dt.timedelta(days=2), ex, hour=23)
    await _train(fresh_db, week + dt.timedelta(days=4), ex)
    comic = await _comic(client, week)
    _check_shape(comic)
    first, second, filler, total = comic["panels"]
    # Первая тренировка в истории — «first», даже ночью.
    assert (first["label"], first["scene"]) == ("СР", "first")
    assert second["scene"] == "bench"
    # Дней два — третий кадр добивает отдых в последний пустой день недели.
    assert (filler["day"], filler["label"], filler["scene"]) == (None, "ВС", "rest")
    # Сравнивать не с чем — неделя закрыта, счёт без «из».
    assert total["scene"] == "week_closed" and "2 тренировки" in total["caption"]


async def test_short_week_gets_exactly_one_skip_last(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, ex)
    await _train(fresh_db, week + dt.timedelta(days=1), ex)
    comic = await _comic(client, week)
    _check_shape(comic)
    assert [(p["label"], p["scene"]) for p in comic["panels"]] == [
        ("ВТ", "bench"), ("СБ", "rest"), ("ВС", "skip"), ("ИТОГ", "week_partial"),
    ]
    assert all(p["day"] is None for p in comic["panels"][1:])
    assert comic["panels"][3]["caption"].startswith("1 из 3.")


async def test_two_days_without_a_norm_rest_only(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    # Две недели назад одна тренировка: «обычного» нет, прошлая неделя — 0.
    await _train(fresh_db, week - dt.timedelta(days=10), ex)
    await _train(fresh_db, week, ex)
    await _train(fresh_db, week + dt.timedelta(days=3), ex)
    comic = await _comic(client, week)
    _check_shape(comic)
    scenes = [p["scene"] for p in comic["panels"]]
    assert scenes == ["bench", "bench", "rest", "week_closed"]


async def test_more_than_three_days_record_first_then_chronological(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    bench = await _exercise(fresh_db, "Жим лёжа")
    curl = await _exercise(fresh_db, "Подъём на бицепс", "Бицепс")
    press = await _exercise(fresh_db, "Жим стоя", "Плечи")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, bench)
    await _train(fresh_db, week, bench, sets=2)             # пн, мало подходов
    await _train(fresh_db, week + dt.timedelta(days=1), curl, weight=20, sets=5)  # вт
    await _train(fresh_db, week + dt.timedelta(days=3), press, weight=40, sets=4)  # чт
    await _train(fresh_db, week + dt.timedelta(days=4), bench, sets=1)   # пт
    await _train(fresh_db, week + dt.timedelta(days=6), bench, weight=90, sets=1)  # вс — рекорд
    comic = await _comic(client, week)
    _check_shape(comic)
    assert [(p["label"], p["scene"], p["record"]) for p in comic["panels"][:3]] == [
        ("ВТ", "curl", False), ("ЧТ", "press", False), ("ВС", "bench", True),
    ]
    total = comic["panels"][3]
    assert total["scene"] == "week_closed" and total["caption"].startswith("5 при обычных 3")


async def test_lb_and_bodyweight_captions(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, lang="en", unit="lb")
    bench = await _exercise(fresh_db, "Bench press")
    pullup = await _exercise(fresh_db, "Pull-up", "Спина")
    week = await _week(fresh_db)
    await _train(fresh_db, week - dt.timedelta(days=5), bench, weight=135)
    await _train(fresh_db, week, bench, weight=185)
    await _train(fresh_db, week + dt.timedelta(days=2), pullup, weight=0, reps=12)
    comic = await _comic(client, week)
    _check_shape(comic)
    mon, wed = comic["panels"][:2]
    assert "Bench press 185lb×5" in mon["caption"] and mon["record"] is True
    assert wed["scene"] == "pull" and "Pull-up ×12 bodyweight" in wed["caption"]
    assert "0lb" not in wed["caption"]


async def test_bodyweight_in_russian(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    pullup = await _exercise(fresh_db, "Подтягивания", "Спина")
    week = await _week(fresh_db)
    await _train(fresh_db, week - dt.timedelta(days=5), pullup, weight=0, reps=8)
    await _train(fresh_db, week, pullup, weight=0, reps=10)
    comic = await _comic(client, week)
    assert "Подтягивания ×10 со своим весом" in comic["panels"][0]["caption"]


async def test_same_week_reads_the_same_on_every_open(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory)
    ex = await _exercise(fresh_db, "Жим лёжа")
    week = await _week(fresh_db)
    await _usual_three(fresh_db, week, ex)
    await _train(fresh_db, week + dt.timedelta(days=1), ex)
    await _train(fresh_db, week + dt.timedelta(days=3), ex, weight=70)
    assert await _comic(client, week) == await _comic(client, week)


# ---------- подписи: чистая функция ----------

def _story(day_offset=0, **kw) -> DayStory:
    base = dict(
        day=dt.date(2026, 10, 26) + dt.timedelta(days=day_offset), pose="bench", hour=10,
        top=SetPick("Жим лёжа", 80.0, 5), work_sets=3,
    )
    base.update(kw)
    return DayStory(**base)


def _facts(stories, this=None, base=3, fillers=(), gentle=False) -> ComicFacts:
    return ComicFacts(
        week_start=dt.date(2026, 10, 26), stories=list(stories),
        this=len(stories) if this is None else this, base=base,
        filler_days=[dt.date(2026, 10, 26) + dt.timedelta(days=d) for d in fillers], gentle=gentle,
    )


def test_no_stories_no_comic():
    assert weekly_comic.build(_facts([], this=0), unit="kg", seed="1") is None


def test_scene_rules_in_order():
    assert weekly_comic.scene_of(_story(is_first=True, hour=23, comeback_weeks=3)) == "first"
    assert weekly_comic.scene_of(_story(comeback_weeks=3, hour=23)) == "comeback"
    assert weekly_comic.scene_of(_story(hour=22)) == "night"
    assert weekly_comic.scene_of(_story(hour=4)) == "night"
    assert weekly_comic.scene_of(_story(hour=5)) == "morning"
    assert weekly_comic.scene_of(_story(hour=6)) == "morning"
    assert weekly_comic.scene_of(_story(hour=7, pose="squat")) == "squat"
    assert weekly_comic.scene_of(_story(hour=21)) == "bench"


def test_pose_by_group():
    assert weekly_comic._pose("Грудь") == "bench"
    assert weekly_comic._pose("Ноги") == "squat"
    assert weekly_comic._pose("Спина") == "pull"
    assert weekly_comic._pose("Плечи") == "press"
    assert weekly_comic._pose("Бицепс") == "curl"
    assert weekly_comic._pose("Трицепс") == "curl"
    assert weekly_comic._pose("Пресс") == weekly_comic.FALLBACK_POSE
    assert weekly_comic._pose(None) == weekly_comic.FALLBACK_POSE


def test_skip_never_twice_and_not_without_a_norm():
    with i18n.use_lang("ru"):
        one = weekly_comic.build(_facts([_story()], base=3, fillers=(3, 4, 5)), unit="kg", seed="s")
        no_norm = weekly_comic.build(_facts([_story()], base=None, fillers=(3, 4)), unit="kg", seed="s")
        gentle = weekly_comic.build(_facts([_story()], base=3, fillers=(3, 4), gentle=True), unit="kg", seed="s")
        met = weekly_comic.build(_facts([_story(), _story(1)], this=3, base=3, fillers=(5,)), unit="kg", seed="s")
    assert [p["scene"] for p in one["panels"]].count("skip") == 1
    assert [p["label"] for p in one["panels"]] == ["ПН", "ПТ", "СБ", "ИТОГ"]  # последние пустые дни
    assert "skip" not in [p["scene"] for p in no_norm["panels"]]
    assert "skip" not in [p["scene"] for p in gentle["panels"]]
    assert "skip" not in [p["scene"] for p in met["panels"]]


def test_filler_without_an_empty_day_gets_a_neutral_label_at_the_end():
    with i18n.use_lang("en"):
        comic = weekly_comic.build(_facts([_story(3)], base=None, fillers=()), unit="kg", seed="s")
    assert [(p["label"], p["scene"]) for p in comic["panels"]] == [
        ("THU", "bench"), ("…", "rest"), ("…", "rest"), ("TOTAL", "week_closed"),
    ]


def test_story_without_sets_still_gets_a_caption():
    with i18n.use_lang("ru"):
        comic = weekly_comic.build(_facts([_story(top=None), _story(1, top=None, hour=23)]), unit="kg", seed="s")
    assert comic["panels"][0]["caption"] == "Тренировка в дневнике. Записал."
    assert comic["panels"][1]["caption"] == "Ночная тренировка. Зал весь твой."


def test_captions_vary_by_seed_but_not_by_reload():
    with i18n.use_lang("ru"):
        facts = _facts([_story(), _story(2), _story(4)])
        a = weekly_comic.build(facts, unit="kg", seed="111:2026-10-26")
        b = weekly_comic.build(facts, unit="kg", seed="111:2026-10-26")
        variants = {
            weekly_comic.build(facts, unit="kg", seed=f"{uid}:2026-10-26")["panels"][0]["caption"]
            for uid in range(40)
        }
    assert a == b
    assert len(variants) == 3


def _every_kind() -> list[ComicFacts]:
    bw = SetPick("X", 0.0, 12)
    return [
        _facts([_story(record=True), _story(1, hour=23), _story(2, hour=6)], base=3),
        _facts([_story(3, hour=23, record=True), _story(4, hour=6, record=True), _story(5, top=bw)], base=2),
        _facts([_story(is_first=True), _story(1, top=None)], base=None, fillers=(5,), gentle=True),
        _facts([_story(comeback_weeks=1)], base=3, fillers=(5, 6), gentle=True),
        _facts([_story(comeback_weeks=5), _story(1, top=None, hour=6)], base=3, fillers=(6,), gentle=True),
        _facts([_story()], base=3, fillers=(5, 6)),
        _facts([_story(), _story(1)], this=4, base=3, fillers=(6,)),
    ]


@pytest.mark.parametrize("lang", ALL_LANGS)
def test_every_caption_kind_in_every_language(lang):
    """Каждая подпись собирается на каждом языке всеми вариантами: без чужого
    языка, без сырых плейсхолдеров, коротко."""
    seen: set[str] = set()
    with i18n.use_lang(lang):
        for facts in _every_kind():
            for uid in range(12):
                for unit in ("kg", "lb"):
                    comic = weekly_comic.build(facts, unit=unit, seed=f"{uid}:w")
                    _check_shape(comic)
                    for p in comic["panels"]:
                        seen.add(p["caption"])
    assert len(seen) > 30
    for caption in seen:
        text = caption.replace("Жим лёжа", "").replace("X", "")
        assert len(text) <= 60, caption
        if lang == "ru":
            assert not [w for w in LATIN_WORD.findall(text) if w not in ("kg", "lb")], caption
        else:
            assert not CYRILLIC.search(text), caption
        assert "ПРИВЕТ, АТЛЕТ" not in text and "HEY, ATHLETE" not in text


def test_every_scene_is_reachable():
    scenes: set[str] = set()
    with i18n.use_lang("en"):
        for facts in _every_kind():
            scenes |= {p["scene"] for p in weekly_comic.build(facts, unit="kg", seed="s")["panels"]}
        for pose in ("squat", "press", "curl", "pull"):
            comic = weekly_comic.build(_facts([_story(pose=pose)], base=1), unit="kg", seed="s")
            scenes |= {p["scene"] for p in comic["panels"]}
    assert scenes == set(weekly_comic.SCENES)
