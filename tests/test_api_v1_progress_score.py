"""GET /v1/progress/score — форма ответа, языки, пустой аккаунт, периоды.

Формулу по правилам держит tests/test_progress_score.py; здесь — что ответ
собран из базы целиком и подписан на языке атлета.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx
import pytest

import api_v1
import api_v1_progress_score
import db
import progress_score
import timeutil

pytestmark = pytest.mark.asyncio

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _client(fresh_db, client_factory, telegram_id: int, lang: str):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester", language_code=lang)
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _template_id(name: str) -> int:
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (name,)
    )
    return (await cur.fetchone())["id"]


async def _workout(user_id: int, day: dt.date, lifts: list[tuple[int, float, int, int]]) -> None:
    wid = await db.create_finished_workout(
        user_id, started_at=f"{day.isoformat()}T10:00:00", finished_at=f"{day.isoformat()}T11:00:00",
    )
    for order, (ex_id, weight, reps, sets) in enumerate(lifts):
        block = await db.create_block(wid, "single")
        await db.add_block_exercise(block, ex_id, order)
        for i in range(sets):
            await db.add_set(block, ex_id, round_index=i + 1, order_in_round=0, weight=weight, reps=reps)


async def _seed_history(user_id: int) -> None:
    """Восемь недель по две тренировки: жим растёт, присед стоит."""
    bench = await db.fork_exercise_from_template(user_id, await _template_id("Жим штанги лёжа"))
    squat = await db.fork_exercise_from_template(user_id, await _template_id("Присед со штангой"))
    today = timeutil.user_today(await db.get_user(user_id))
    for n in range(56, -1, -3):
        day = today - dt.timedelta(days=n)
        await _workout(user_id, day, [(bench, 80 + (56 - n) / 4, 5, 4), (squat, 100, 5, 4)])


def _texts(body) -> list[str]:
    out = [body["title"], body["coach_line"], body["widget_line"]]
    out += [p["label"] for p in body["parts"]] + [p["detail"] for p in body["parts"]]
    out += [lift["name"] for lift in body["lifts"]] + [g["name"] for g in body["groups"]]
    return out


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_shape_and_language(fresh_db, client_factory, lang):
    client = await _client(fresh_db, client_factory, 111, lang)
    await _seed_history(111)
    resp = await client.get("/progress/score")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert 0 <= body["score"] <= 10
    assert body["unit"] == "kg" and body["period_days"] == 28
    assert [p["key"] for p in body["parts"]] == ["progression", "regularity", "volume"]
    assert [p["max"] for p in body["parts"]] == [5, 3, 2]
    assert len(body["history"]) == 5  # точка в неделю
    assert set(body["history"][0]) == {"date", "score", "delta_7d", "progression", "regularity", "volume"}
    assert body["history"][-1]["score"] == body["score"]
    assert body["best"]["score"] >= body["score"]
    assert isinstance(body["delta_7d"], float)
    assert len(body["weeks"]) == 4 and all(w["counted"] for w in body["weeks"])
    assert body["calibration"] is None and body["paused"] is None and body["progression_from"] is None
    lifts = {lift["exercise_id"]: lift for lift in body["lifts"]}
    assert len(lifts) == 2 and all(lift["rated"] for lift in lifts.values())
    # Присед стоит (шум) — он слабее растущего жима и идёт первым.
    assert body["lifts"][0]["change_pct"] == 0.0 and body["lifts"][0]["points"] == 0.3
    assert body["lifts"][1]["change_pct"] > 1
    assert all(lift["baseline_reps"] is None and lift["current_reps"] is None for lift in body["lifts"])
    assert body["widget_line"]
    assert {g["name"] for g in body["groups"]} == ({"Грудь", "Ноги"} if lang == "ru" else {"Chest", "Legs"})
    _assert_groups_match_volume(body)
    texts = _texts(body)
    if lang == "en":
        assert not any(CYRILLIC.search(t) for t in texts), texts
        assert body["title"] == "SCORE"
    else:
        assert body["title"] == "ОЦЕНКА"
        assert all(CYRILLIC.search(t) for t in texts if t), texts


def _assert_groups_match_volume(body) -> None:
    """Карточка объёма рисует ровно то, что в балле: среднее за неделю — по
    неделям в счёте без разгрузок, подходы по неделям — в порядке `weeks`."""
    used = [i for i, w in enumerate(body["weeks"]) if w["counted"] and not w["deload"]]
    shares = []
    for g in body["groups"]:
        assert isinstance(g["sets_per_week"], float)
        assert len(g["week_sets"]) == len(body["weeks"]) and all(isinstance(n, int) for n in g["week_sets"])
        assert sum(g["week_sets"]) == g["sets_28d"]
        assert g["sets_per_week"] == round(sum(g["week_sets"][i] for i in used) / len(used), 1)
        shares.append(sum(min(g["week_sets"][i], 6) / 6 for i in used) / len(used))
    volume = next(p for p in body["parts"] if p["key"] == "volume")
    assert volume["points"] == round(2 * sum(shares) / len(shares), 1)
    # Подпись считает группы с полным баллом: от 6 подходов в каждой из этих недель.
    full = sum(1 for g in body["groups"] if all(g["week_sets"][i] >= 6 for i in used))
    if not body["deload"]:
        assert f" {full} " in volume["detail"], volume["detail"]


async def test_empty_account(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    body = (await client.get("/progress/score")).json()
    assert body["status"] == "empty" and body["score"] is None and body["delta_7d"] is None
    assert body["coach_line"] == "Сделай тренировку — посчитаю оценку."
    assert body["widget_line"] == "Сделай первую тренировку"
    assert all(p["points"] is None and p["detail"] == "" for p in body["parts"])
    assert body["lifts"] == [] and body["groups"] == [] and body["best"] is None
    assert len(body["history"]) == 5 and all(h["score"] is None for h in body["history"])


async def test_newcomer_calibrating(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "en")
    bench = await db.fork_exercise_from_template(111, await _template_id("Жим штанги лёжа"))
    today = timeutil.user_today(await db.get_user(111))
    await _workout(111, today - dt.timedelta(days=3), [(bench, 60, 5, 3)])
    body = (await client.get("/progress/score")).json()
    assert body["status"] == "calibrating" and body["score"] is None
    ready = today - dt.timedelta(days=3) + dt.timedelta(days=28)
    assert body["calibration"] == {
        "ready_on": ready.isoformat(), "days_done": 3, "days_total": 28, "workouts": 1,
    }
    assert body["parts"][1]["points"] is not None
    assert body["coach_line"].startswith("I'll give you your first score on")
    assert not CYRILLIC.search(body["coach_line"])


@pytest.mark.parametrize("days, points", [("28", 5), ("90", 13), ("365", 53)])
async def test_longer_periods(fresh_db, client_factory, days, points):
    client = await _client(fresh_db, client_factory, 111, "ru")
    await _seed_history(111)
    body = (await client.get(f"/progress/score?days={days}")).json()
    assert body["period_days"] == int(days) and len(body["history"]) == points
    dates = [dt.date.fromisoformat(h["date"]) for h in body["history"]]
    assert all((b - a).days == 7 for a, b in zip(dates, dates[1:], strict=False))
    if days != "28":
        assert body["history"][0]["score"] is None  # до первой тренировки оценки нет


@pytest.mark.parametrize("days", ["7", "soon", "0"])
async def test_bad_period_is_human_400(fresh_db, client_factory, days):
    client = await _client(fresh_db, client_factory, 111, "ru")
    resp = await client.get(f"/progress/score?days={days}")
    assert resp.status_code == 400
    assert resp.json()["message"] == "Период выбирай из трёх: 28, 90 или 365 дней."


async def test_history_delta_is_against_a_week_before_each_point(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    await _seed_history(111)
    long = (await client.get("/progress/score?days=90")).json()["history"]
    by_date = {h["date"]: h for h in long}
    for prev, point in zip(long, long[1:], strict=False):
        if prev["score"] is None or point["score"] is None:
            assert point["delta_7d"] is None
        else:
            assert point["delta_7d"] == round(point["score"] - prev["score"], 1)
    assert long[0]["delta_7d"] is None  # неделей раньше тренировок не было
    # Самая старая точка короткого ряда тоже знает свою прошлую неделю.
    short = (await client.get("/progress/score")).json()
    for point in short["history"]:
        assert point["delta_7d"] == by_date[point["date"]]["delta_7d"]
    assert short["history"][-1]["delta_7d"] == short["delta_7d"]


async def test_as_of_is_the_report_of_that_day(fresh_db, client_factory, monkeypatch):
    client = await _client(fresh_db, client_factory, 111, "ru")
    await _seed_history(111)
    now = (await client.get("/progress/score?days=90")).json()
    loads = []
    real = db.progress_score_sets

    async def counted(*args, **kwargs):
        loads.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(db, "progress_score_sets", counted)
    point = now["history"][-3]
    week = (await client.get(f"/progress/score?as_of={point['date']}")).json()
    assert len(loads) == 1  # одна загрузка подходов, как без as_of
    assert week["score"] == point["score"] and week["delta_7d"] == point["delta_7d"]
    assert week["history"][-1]["date"] == point["date"] and len(week["history"]) == 5
    assert week["weeks"][0]["end"] >= point["date"] >= week["weeks"][0]["start"]
    assert (await client.get(f"/progress/score?as_of={point['date']}&days=90")).json()["period_days"] == 90
    # До первой тренировки — не ошибка, а пустая оценка.
    first = dt.date.fromisoformat(now["history"][0]["date"])
    empty = (await client.get(f"/progress/score?as_of={first.isoformat()}")).json()
    assert empty["status"] == "empty" and empty["score"] is None


@pytest.mark.parametrize("shift, raw", [(1, None), (366, None), (None, "soon"), (None, "2026-1-1")])
async def test_bad_as_of_is_human_400(fresh_db, client_factory, shift, raw):
    client = await _client(fresh_db, client_factory, 111, "ru")
    today = timeutil.user_today(await db.get_user(111))
    if shift == 1:
        raw = (today + dt.timedelta(days=1)).isoformat()
    elif shift:
        raw = (today - dt.timedelta(days=shift)).isoformat()
    resp = await client.get(f"/progress/score?as_of={raw}")
    assert resp.status_code == 400
    assert resp.json()["message"] == "Эту неделю не открою: выбери день за последний год, не позже сегодня."
    assert (await client.get(f"/progress/score?as_of={(today - dt.timedelta(days=365)).isoformat()}")).status_code == 200


async def test_calibration_script_reads_a_file_db(tmp_path, capsys):
    """scripts/progress_score_report.py: база только на чтение, числа без id."""
    import asyncio
    import importlib.util

    path = str(tmp_path / "bot.db")
    db._write_lock = asyncio.Lock()
    await db.init_db(path)
    try:
        await db.get_or_create_user(telegram_id=4242, username="athlete")
        await _seed_history(4242)
    finally:
        await db.close_db()
    spec = importlib.util.spec_from_file_location("progress_score_report", "scripts/progress_score_report.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    script.main(path, 28)
    out = capsys.readouterr().out
    assert "атлетов с тренировкой за 28 дн.: 1" in out
    assert "статусы сегодня: ok 1" in out
    assert "4242" not in out


# --- реплики и подписи ----------------------------------------------------------


async def _seed_steady(user_id: int, lifts: list[tuple[int, float, int, int]], weeks: int = 10,
                       weekdays: tuple[int, ...] = (0, 3), skip: tuple[int, ...] = ()) -> dt.date:
    """Тренировки по `weekdays` календарной недели за `weeks` недель; `skip` —
    номера недель назад (0 — текущая), в которых только одна тренировка."""
    today = timeutil.user_today(await db.get_user(user_id))
    monday = today - dt.timedelta(days=today.weekday())
    for n in range(weeks * 7, -1, -1):
        day = today - dt.timedelta(days=n)
        week_back = (monday - (day - dt.timedelta(days=day.weekday()))).days // 7
        if day.weekday() in weekdays and not (week_back in skip and day.weekday() != weekdays[0]):
            await _workout(user_id, day, lifts)
    return today


async def test_all_good_coach_and_widget_lines(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    bench = await db.fork_exercise_from_template(111, await _template_id("Жим штанги лёжа"))
    today = timeutil.user_today(await db.get_user(111))
    # Жим растёт на +6% за окно, две тренировки в неделю, по 6 подходов: всё по плану.
    for n in range(70, -1, -1):
        day = today - dt.timedelta(days=n)
        if day.weekday() in (0, 3) or n == 0:
            await _workout(111, day, [(bench, 100 + (70 - n) / 5, 5, 6)])
    body = (await client.get("/progress/score")).json()
    assert body["status"] == "ok" and body["score"] == 10.0, body
    assert body["coach_line"] == "Всё по плану — так и держи."
    assert body["widget_line"] == "Всё по плану"


async def test_widget_line_never_names_a_lift(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    await _seed_history(111)
    body = (await client.get("/progress/score")).json()
    names = [lift["name"] for lift in body["lifts"]] + [g["name"] for g in body["groups"]]
    assert body["coach_line"] and body["widget_line"]
    assert not any(name in body["widget_line"] for name in names)
    assert any(name in body["coach_line"] for name in names)  # а реплика экрана — называет


@pytest.mark.parametrize("lang", ["ru", "en", "es", "pt", "pt-PT", "de", "fr", "tr"])
def test_widget_lines_are_short_and_cover_every_lever(lang):
    import i18n
    with i18n.use_lang(lang):
        for kind in api_v1_progress_score._WIDGET_KEY:
            lever = progress_score.Lever(kind) if kind else None
            line = api_v1_progress_score.widget_line(lever)
            # Вторая строка виджета-плитки на «Меню» держит ~26 знаков
            # кириллицы на ширине iPhone, дальше — «…» («Рост в силе
            # посчитаю п…» на снимке прохода 22 ноября). Запас — до 24;
            # остальным языкам — на пятую часть длиннее (TONE_OF_VOICE).
            assert line and len(line) <= (24 if lang in ("ru", "en") else 28), (lang, kind, line)


async def test_regularity_detail_matches_half_credit(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    bench = await db.fork_exercise_from_template(111, await _template_id("Жим штанги лёжа"))
    # Неделю назад — одна тренировка вместо двух: половина балла, и подпись это говорит.
    await _seed_steady(111, [(bench, 100, 5, 6)], skip=(1,))
    body = (await client.get("/progress/score")).json()
    reg = next(p for p in body["parts"] if p["key"] == "regularity")
    assert reg["points"] == 2.6  # 3 × (1 + 0.5 + 1 + 1) / 4
    assert reg["detail"] == "две тренировки и больше — 3 из 4 недель, в одной — одна"


async def test_pullups_only_athlete_is_rated_by_reps(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "en")
    pullups = await db.fork_exercise_from_template(111, await _template_id("Подтягивания"))
    today = timeutil.user_today(await db.get_user(111))
    for n in range(63, -1, -1):
        day = today - dt.timedelta(days=n)
        if day.weekday() in (0, 3):
            await _workout(111, day, [(pullups, 0, 8 + (63 - n) // 14, 4)])
    body = (await client.get("/progress/score")).json()
    assert body["status"] == "ok"
    lift = body["lifts"][0]
    assert lift["rated"] and lift["baseline_e1rm"] is None and lift["current_e1rm"] is None
    assert isinstance(lift["baseline_reps"], int) and lift["current_reps"] > lift["baseline_reps"]
    prog = body["parts"][0]
    assert prog["points"] is not None and prog["detail"] == "you're progressing in 1 of 1 lift"


async def test_progression_none_detail_is_about_workouts(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    plank = await db.fork_exercise_from_template(111, await _template_id("Планка"))
    await _seed_steady(111, [(plank, 0, 60, 3)], weeks=8)
    body = (await client.get("/progress/score")).json()
    prog = body["parts"][0]
    assert prog["points"] is None
    assert prog["detail"] == "посчитаю, когда наберётся месяц тренировок"
