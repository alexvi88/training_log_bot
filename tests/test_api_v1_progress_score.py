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
import db
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
    out = [body["title"], body["coach_line"]]
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
    assert [p["key"] for p in body["parts"]] == ["progression", "regularity", "volume", "balance"]
    assert [p["max"] for p in body["parts"]] == [4, 3, 2, 1]
    assert len(body["history"]) == 28
    assert set(body["history"][0]) == {"date", "score", "progression", "regularity", "volume", "balance"}
    assert body["history"][-1]["score"] == body["score"]
    assert body["best"]["score"] >= body["score"]
    assert isinstance(body["delta_7d"], float)
    assert len(body["weeks"]) == 4 and all(w["counted"] for w in body["weeks"])
    assert body["calibration"] is None and body["paused"] is None and body["progression_from"] is None
    lifts = {lift["exercise_id"]: lift for lift in body["lifts"]}
    assert len(lifts) == 2 and all(lift["rated"] for lift in lifts.values())
    # Присед стоит (шум) — он слабее растущего жима и идёт первым.
    assert body["lifts"][0]["change_pct"] == 0.0 and body["lifts"][0]["points"] == 0.5
    assert body["lifts"][1]["change_pct"] > 1
    assert {g["name"] for g in body["groups"]} == ({"Грудь", "Ноги"} if lang == "ru" else {"Chest", "Legs"})
    texts = _texts(body)
    if lang == "en":
        assert not any(CYRILLIC.search(t) for t in texts), texts
        assert body["title"] == "SCORE"
    else:
        assert body["title"] == "ОЦЕНКА"
        assert all(CYRILLIC.search(t) for t in texts if t), texts


async def test_empty_account(fresh_db, client_factory):
    client = await _client(fresh_db, client_factory, 111, "ru")
    body = (await client.get("/progress/score")).json()
    assert body["status"] == "empty" and body["score"] is None and body["delta_7d"] is None
    assert body["coach_line"] == "Сделай тренировку — посчитаю оценку."
    assert all(p["points"] is None and p["detail"] == "" for p in body["parts"])
    assert body["lifts"] == [] and body["groups"] == [] and body["best"] is None
    assert len(body["history"]) == 28 and all(h["score"] is None for h in body["history"])


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


@pytest.mark.parametrize("days, points", [("90", 13), ("365", 53)])
async def test_longer_periods(fresh_db, client_factory, days, points):
    client = await _client(fresh_db, client_factory, 111, "ru")
    await _seed_history(111)
    body = (await client.get(f"/progress/score?days={days}")).json()
    assert body["period_days"] == int(days) and len(body["history"]) == points
    dates = [dt.date.fromisoformat(h["date"]) for h in body["history"]]
    assert all((b - a).days == 7 for a, b in zip(dates, dates[1:], strict=False))
    assert body["history"][0]["score"] is None  # до первой тренировки оценки нет


@pytest.mark.parametrize("days", ["7", "soon", "0"])
async def test_bad_period_is_human_400(fresh_db, client_factory, days):
    client = await _client(fresh_db, client_factory, 111, "ru")
    resp = await client.get(f"/progress/score?days={days}")
    assert resp.status_code == 400
    assert resp.json()["message"] == "Период выбирай из трёх: 28, 90 или 365 дней."


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
