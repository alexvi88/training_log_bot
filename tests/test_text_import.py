"""Импорт из произвольного текста: заметки → подходы (модель) → CSV → тот же
разбор, что у файла Hevy/Strong (text_import.py, POST /import/text/convert).

Модель подменена: проверяется всё, что вокруг неё и что решает код, — резка
на куски с передачей даты, отбраковка ответа, пересчёт единиц и то, что
собранный CSV проходит обычные /import/csv/preview и /import/csv.
"""

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

import ai_trainer
import api_v1
import api_v1_import
import db
import text_import

TODAY = dt.date(2026, 9, 29)


def _response(sets):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"sets": sets})))],
        usage=None,
    )


class _FakeCompletions:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(json.loads(kwargs["messages"][1]["content"]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _response(answer)


def _install(monkeypatch, answers):
    completions = _FakeCompletions(answers)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def no_cost(*a, **k):
        return None

    monkeypatch.setattr(ai_trainer, "_log_llm_cost", no_cost)
    return completions


def _set(date, name, weight, reps, unit="unknown"):
    return {"date": date, "exercise": name, "weight": weight, "unit": unit, "reps": reps}


# ---------- чистая логика ----------


def test_chunks_split_on_line_boundaries_only():
    text = "\n".join(f"line {i} 100 8" for i in range(100))
    pieces = text_import._chunks(text, limit=200)
    assert len(pieces) > 1
    assert all(len(p) <= 200 for p in pieces)
    assert "\n".join(pieces).split("\n") == text.split("\n")


def test_chunks_drop_blank_only_pieces():
    assert text_import._chunks("\n\n  \n") == []


def test_clean_rows_rejects_nonsense_and_counts_undated():
    rows, undated = text_import._clean_rows(
        [
            _set("2026-03-01", "  Жим   лёжа ", 100, 8),
            _set("2026-03-01", "", 100, 8),          # без имени
            _set("2026-03-01", "Присед", -5, 5),      # отрицательный вес
            _set("2026-03-01", "Присед", 100, 0),     # ноль повторов
            _set("2027-01-01", "Присед", 100, 5),     # будущее
            _set(None, "Присед", 100, 5),             # без даты
            _set("12 марта", "Присед", 100, 5),       # не ISO
            "garbage",
        ],
        TODAY,
    )
    assert rows == [
        {"date": "2026-03-01", "exercise": "Жим лёжа", "weight": 100.0, "unit": None, "reps": 8},
        # Без даты — не брак: дату проставит _stitch_dates по куску выше.
        {"date": None, "exercise": "Присед", "weight": 100.0, "unit": None, "reps": 5},
    ]
    assert undated == 2


def test_stitch_dates_fills_from_above_and_counts_orphans():
    def row(date):
        return {"date": date, "exercise": "Жим", "weight": 100.0, "unit": None, "reps": 5}

    rows, undated = text_import._stitch_dates([row(None), row("2026-03-01"), row(None), row("2026-03-03"), row(None)])
    assert [r["date"] for r in rows] == ["2026-03-01", "2026-03-01", "2026-03-03", "2026-03-03"]
    assert undated == 1


def test_rows_to_csv_orders_by_date_and_converts_marked_units():
    rows = [
        {"date": "2026-03-02", "exercise": "Присед", "weight": 225.0, "unit": "lb", "reps": 5},
        {"date": "2026-03-01", "exercise": "Жим, узкий", "weight": 100.0, "unit": None, "reps": 8},
        {"date": "2026-03-01", "exercise": "Подтягивания", "weight": 0.0, "unit": None, "reps": 10},
    ]
    assert text_import.rows_to_csv(rows, "kg").splitlines() == [
        "date,exercise,weight,reps",
        '2026-03-01,"Жим, узкий",100,8',
        "2026-03-01,Подтягивания,0,10",
        "2026-03-02,Присед,102.06,5",
    ]
    lb = text_import.rows_to_csv([{"date": "2026-03-01", "exercise": "Жим", "weight": 100.0, "unit": "kg", "reps": 5}], "lb")
    assert lb.splitlines()[1] == "2026-03-01,Жим,220.46,5"


async def test_extract_stitches_date_across_chunks(monkeypatch):
    monkeypatch.setattr(text_import, "CHUNK_CHARS", 30)
    # Три строки по ~25 символов при куске в 30 — ровно три куска; ответы
    # раздаются по содержимому куска, а не по порядку: куски идут параллельно.
    text = "01.03 жим 100 8 ........\nжим 100 7 ..............\n03.03 присед 120 5 .....\n"
    assert len(text_import._chunks(text)) == 3
    by_piece = {
        "01.03": [_set("2026-03-01", "Жим", 100, 8)],
        "жим 100 7": [_set(None, "Жим", 100, 7)],
        "03.03": [_set("2026-03-03", "Присед", 120, 5)],
    }
    completions = _install(monkeypatch, [])

    async def create(**kwargs):
        notes = json.loads(kwargs["messages"][1]["content"])
        completions.calls.append(notes)
        key = next(k for k in by_piece if notes["notes"].startswith(k))
        return _response(by_piece[key])

    completions.create = create
    result = await text_import.extract_sets(1, text, TODAY)
    assert [(r["date"], r["reps"]) for r in result.rows] == [("2026-03-01", 8), ("2026-03-01", 7), ("2026-03-03", 5)]
    assert result.undated == 0
    assert all(c["today"] == "2026-09-29" for c in completions.calls)


async def test_extract_survives_garbage_answer(monkeypatch):
    completions = _install(monkeypatch, [])

    async def garbage(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))], usage=None)

    completions.create = garbage
    result = await text_import.extract_sets(1, "жим 100 8", TODAY)
    assert result.rows == [] and result.undated == 0


# ---------- REST ----------


async def _client(fresh_db, uid, unit="kg", consent=True):
    await fresh_db.get_or_create_user(uid, "t")
    await fresh_db.update_user(uid, unit=unit)
    code = await fresh_db.issue_oauth_link_code(uid, ttl_seconds=600, digits=8)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    tok = (await c.post("/auth/link", json={"code": code})).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    c.headers["X-AI-Consent-Flow"] = "1"
    if consent:
        assert (await c.patch("/settings", json={"ai_consent": True})).status_code == 200
    return c


async def test_convert_then_regular_csv_import(fresh_db, monkeypatch):
    uid = 901
    _install(monkeypatch, [[
        _set(None, "Тяга", 140, 3),
        _set("2026-03-01", "Жим лёжа", 100, 8),
        _set("2026-03-01", "Жим лёжа", 100, 7),
        _set("2026-03-03", "Присед", 120, 5),
    ]])
    c = await _client(fresh_db, uid)
    r = await c.post("/import/text/convert", json={"text": "тяга 140 3\n01.03 жим 100 8, 100 7\n03.03 присед 120 5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["set_count"] == 3 and body["undated_sets"] == 1

    # Сопоставление названий с каталогом моделью здесь ни к чему — модель
    # уже подменена под разбор текста, дальше работаем без неё.
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    r = await c.post("/import/csv/preview", json={"csv": body["csv"]})
    assert r.status_code == 200, r.text
    assert r.json()["workout_count"] == 2 and r.json()["set_count"] == 3
    r = await c.post("/import/csv", json={"csv": body["csv"]})
    assert r.status_code == 200, r.text
    assert r.json()["workouts_imported"] == 2
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id WHERE w.user_id = ?",
        (uid,),
    )
    assert (await cur.fetchone())[0] == 3


async def test_convert_requires_ai_consent(fresh_db, monkeypatch):
    completions = _install(monkeypatch, [])
    c = await _client(fresh_db, 902, consent=False)
    r = await c.post("/import/text/convert", json={"text": "жим 100 8"})
    assert r.status_code == 403
    assert r.json()["error"] == "ai_consent_required"
    assert completions.calls == []


async def test_convert_rejects_empty_and_huge_text_without_model(fresh_db, monkeypatch):
    completions = _install(monkeypatch, [])
    c = await _client(fresh_db, 903)
    r = await c.post("/import/text/convert", json={"text": "   "})
    assert r.status_code == 400 and r.json()["message"]
    r = await c.post("/import/text/convert", json={"text": "жим 100 8\n" * 10_000})
    assert r.status_code == 413 and r.json()["error"] == "text_too_large"
    assert completions.calls == []


async def test_convert_explains_missing_sets_and_dates(fresh_db, monkeypatch):
    _install(monkeypatch, [[], [_set(None, "Жим", 100, 8)]])
    c = await _client(fresh_db, 904)
    r = await c.post("/import/text/convert", json={"text": "купить молоко"})
    assert r.status_code == 400 and r.json()["error"] == "no_sets_found"
    no_sets = r.json()["message"]
    r = await c.post("/import/text/convert", json={"text": "жим 100 8"})
    assert r.status_code == 400 and r.json()["error"] == "no_sets_found"
    assert r.json()["message"] != no_sets


async def test_convert_model_failure_is_502_and_releases_lock(fresh_db, monkeypatch):
    _install(monkeypatch, [RuntimeError("provider down"), [_set("2026-03-01", "Жим", 100, 8)]])
    c = await _client(fresh_db, 905)
    r = await c.post("/import/text/convert", json={"text": "жим 100 8"})
    assert r.status_code == 502 and r.json()["error"] == "text_import_failed"
    assert 905 not in api_v1_import._converting
    r = await c.post("/import/text/convert", json={"text": "01.03 жим 100 8"})
    assert r.status_code == 200, r.text


async def test_convert_is_one_at_a_time_per_athlete(fresh_db, monkeypatch):
    completions = _install(monkeypatch, [])
    c = await _client(fresh_db, 906)
    api_v1_import._converting.add(906)
    try:
        r = await c.post("/import/text/convert", json={"text": "жим 100 8"})
    finally:
        api_v1_import._converting.discard(906)
    assert r.status_code == 409 and r.json()["error"] == "import_in_progress"
    assert completions.calls == []


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_convert_errors_speak_athletes_language(fresh_db, monkeypatch, lang):
    _install(monkeypatch, [[]])
    c = await _client(fresh_db, 907)
    await c.patch("/settings", json={"lang": lang})
    r = await c.post("/import/text/convert", json={"text": "hello"})
    message = r.json()["message"]
    has_cyrillic = any("а" <= ch.lower() <= "я" for ch in message)
    assert has_cyrillic == (lang == "ru")


# ---------- порядок числовых дат по языку атлета ----------


def _fake_reader(order_seen: list, prompts_seen: list):
    """Подменённая модель, которая читает дату ровно по инструкции из входа:
    «a/b», «a.b» — по numeric_date_order, «Mar 12» — по названию месяца.
    Проверяется не модель, а то, что до неё доезжает язык атлета и что
    системный промпт от языка не меняется."""
    months = {"mar": 3}

    async def create(**kwargs):
        prompts_seen.append(kwargs["messages"][0]["content"])
        payload = json.loads(kwargs["messages"][1]["content"])
        order_seen.append(payload["numeric_date_order"])
        head, _, rest = payload["notes"].partition(" ")
        word = head.lower()
        if word in months:
            day, _, rest = rest.partition(" ")
            month = months[word]
            day = int(day)
        else:
            a, b = (int(x) for x in head.replace(".", "/").split("/"))
            month, day = (a, b) if payload["numeric_date_order"] == "month/day" else (b, a)
        date = dt.date(2026, month, day)
        if date > dt.date.fromisoformat(payload["today"]):
            date = date.replace(year=2025)
        name, weight, reps = rest.split()
        return _response([_set(date.isoformat(), name, float(weight), int(reps))])

    return create


def test_numeric_date_order_by_language():
    assert text_import.numeric_date_order("en") == "month/day"
    assert text_import.numeric_date_order("en-US") == "month/day"
    assert text_import.numeric_date_order("ru") == "day.month"
    assert text_import.numeric_date_order(None) == "day.month"


@pytest.mark.parametrize(
    "lang,notes,expected",
    [
        ("en", "03/12 Bench 100 5", "2026-03-12"),
        ("ru", "03.12 Жим 100 5", "2025-12-03"),
        ("en", "Mar 12 Bench 100 5", "2026-03-12"),
        ("ru", "Mar 12 Жим 100 5", "2026-03-12"),
    ],
)
async def test_convert_reads_numeric_dates_in_athlete_order(fresh_db, monkeypatch, lang, notes, expected):
    uid = 950 + len(notes) + (0 if lang == "ru" else 20)
    completions = _install(monkeypatch, [])
    order_seen: list = []
    prompts_seen: list = []
    completions.create = _fake_reader(order_seen, prompts_seen)
    c = await _client(fresh_db, uid)
    await fresh_db.set_user_lang(uid, lang)
    r = await c.post("/import/text/convert", json={"text": notes})
    assert r.status_code == 200, r.text
    assert r.json()["csv"].splitlines()[1].startswith(expected + ",")
    assert order_seen == ["month/day" if lang == "en" else "day.month"]
    # Системный промпт — общий кэшируемый префикс, язык в нём не живёт.
    assert prompts_seen == [text_import._SYSTEM_PROMPT]
