"""Случаи из аудита импорта CSV (файлы Strong/Hevy/Excel, прогнанные через
REST `/v1/import/csv*` и разбор бота): каждый тест — реальная форма файла,
на которой импорт раньше молча писал неправду или валился целиком.

Разбор общий у бота и REST (handlers/csv_import.py), поэтому большинство
случаев проверено на _build_workout_groups напрямую, а сквозные — там, где
баг жил в самом REST (согласие на AI, дубли упражнений, поля превью).
"""

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import ai_trainer
import api_v1
import i18n
import timeutil
from handlers import csv_import
from handlers.csv_import import (
    _auto_detect,
    _build_workout_groups,
    _parse_row_date,
    _read_table,
    _weight_factor,
)
from parser import ParseError

MAPPING = {"date": 0, "exercise": 1, "weight": 2, "reps": 3}
TODAY = dt.date(2026, 9, 25)


def _groups(text: str, today: dt.date = TODAY, stats: dict | None = None) -> list[dict]:
    headers, rows, has_header = _read_table(text)
    mapping = _auto_detect(headers)
    return _build_workout_groups(
        rows, mapping, first_line=2 if has_header else 1, today=today,
        weight_factor=_weight_factor(headers, mapping, "kg"), stats=stats,
    )


# ---------- 1. будущее и слишком старые даты — во всех форматах ----------


@pytest.mark.parametrize("text", ["2099-01-01", "2026-10-25T08:00:00", "25 Oct 2026, 08:27", "26.09.2026"])
def test_future_date_is_rejected_in_every_format(text):
    with i18n.use_lang("ru"):
        with pytest.raises(ParseError) as excinfo:
            _parse_row_date(text, TODAY)
        assert excinfo.value.message == i18n.t("input.date_in_future")


@pytest.mark.parametrize("text", ["1970-01-01", "1999-12-31T23:00:00", "1 Jan 1999", "31.12.1999"])
def test_date_before_2000_is_rejected_in_every_format(text):
    for lang in ("ru", "en"):
        with i18n.use_lang(lang):
            with pytest.raises(ParseError) as excinfo:
                _parse_row_date(text, TODAY)
            assert excinfo.value.message == i18n.t("import.err_date_too_old", text=text)


def test_user_today_is_the_bound_for_iso_too():
    """Та же поправка на пояс, что у дд.мм: своё «сегодня» у атлета на
    UTC+14 — это ещё «завтра» по серверу, и отказывать ему нельзя."""
    tomorrow = TODAY + dt.timedelta(days=1)
    assert _parse_row_date(tomorrow.isoformat(), tomorrow) == tomorrow
    with pytest.raises(ParseError):
        _parse_row_date(tomorrow.isoformat(), TODAY)


def test_first_row_with_a_future_date_is_still_data_not_a_header():
    """Файл без заголовков: строка с датой в будущем — это строка данных, и
    ошибка про неё должна прийти со своим номером, а не превратить её в
    заголовки и молча потерять."""
    headers, rows, has_header = _read_table("2099-01-01,Жим,80,8\n2024-02-01,Жим,80,8\n")
    assert has_header is False
    assert len(rows) == 2


# ---------- 2. фунты у Hevy (weight_lbs) ----------


HEVY_LBS = (
    "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
    "set_index,set_type,weight_lbs,reps,distance_km,duration_seconds,rpe\n"
    'Upper A,"7 Aug 2026, 08:27","7 Aug 2026, 09:31",,Bench Press (Barbell),,,1,normal,225,5,,,\n'
)


def test_hevy_weight_lbs_is_auto_detected_and_converted_to_kg():
    headers, _rows, _ = _read_table(HEVY_LBS)
    mapping = _auto_detect(headers)
    assert mapping["weight"] == headers.index("weight_lbs")
    (workout,) = _groups(HEVY_LBS)
    assert workout["entries"][0]["sets"] == [[102.1, 5, None]]


@pytest.mark.parametrize(
    "header, is_lbs",
    [
        ("weight_lbs", True), ("weight_lb", True), ("Weight (lbs)", True), ("Weight lbs", True),
        ("LB", True), ("Weight (pounds)", True),
        ("weight_kg", False), ("Weight (kg)", False), ("Weight", False), ("bulbs", False),
    ],
)
def test_pounds_are_recognized_by_suffix_and_word(header, is_lbs):
    """Та же проверка работает и после ручного выбора колонки в боте:
    _finish_mapping зовёт _weight_factor с заголовком выбранной колонки."""
    factor = _weight_factor(["date", "exercise", header, "reps"], MAPPING, "kg")
    assert (factor != 1.0) is is_lbs


# ---------- 3. разминка и буквенный номер подхода ----------


STRONG_HEADER = "Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE\n"


def test_strong_letter_set_orders():
    """W — разминка (пропуск, не ошибка), D/F — рабочие подходы без номера.
    Раньше любая буква валила весь файл на «не понял номер подхода»."""
    text = STRONG_HEADER + (
        "2024-01-15 18:30:00,Push,1h,Bench Press (Barbell),W,40,10,0,0,,,\n"
        "2024-01-15 18:30:00,Push,1h,Bench Press (Barbell),1,100,5,0,0,,,\n"
        "2024-01-15 18:30:00,Push,1h,Bench Press (Barbell),F,100,3,0,0,,,\n"
        "2024-01-15 18:30:00,Push,1h,Bench Press (Barbell),D,70,8,0,0,,,\n"
        "2024-01-15 18:30:00,Push,1h,Plank,1,0,0,0,60,,,\n"
    )
    stats: dict = {}
    (workout,) = _groups(text, stats=stats)
    (bench,) = workout["entries"]
    assert bench["sets"] == [[100.0, 5, None], [100.0, 3, None], [70.0, 8, None]]
    assert stats["rows_skipped_warmup"] == 1
    assert stats["rows_skipped_no_load"] == 1
    assert stats["rows_skipped"] == 2


def test_warmup_only_file_is_no_sets_found_not_an_error_per_row():
    text = STRONG_HEADER + "2024-01-15 18:30:00,Push,1h,Bench Press (Barbell),W,40,10,0,0,,,\n"
    assert _groups(text) == []


def test_hevy_set_type_warmup_is_skipped():
    header = (
        "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
        "set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n"
    )
    text = header + (
        'A,"7 Aug 2026, 08:27",,,Bench Press (Barbell),,,0,warmup,40,12,,,\n'
        'A,"7 Aug 2026, 08:27",,,Bench Press (Barbell),,,1,normal,100,5,,,\n'
        'A,"7 Aug 2026, 08:27",,,Bench Press (Barbell),,,2,dropset,80,8,,,\n'
    )
    stats: dict = {}
    (workout,) = _groups(text, stats=stats)
    assert workout["entries"][0]["sets"] == [[100.0, 5, None], [80.0, 8, None]]
    assert stats["rows_skipped_warmup"] == 1


# ---------- 4. американские даты ----------


def _dates(values: list[str], stats: dict | None = None) -> list[str]:
    rows = [[v, "Bench Press", "100", "5"] for v in values]
    workouts = _build_workout_groups(rows, MAPPING, today=TODAY, stats=stats)
    return [w["date"] for w in workouts]


def test_us_column_is_read_month_first_when_any_day_exceeds_12():
    stats: dict = {}
    # «3/4/2024» сам по себе неоднозначен — решает соседняя «3/15/2024».
    assert _dates(["3/4/2024", "3/15/2024"], stats) == ["2024-03-04", "2024-03-15"]
    assert stats["date_order"] == "mdy"
    assert stats["date_order_ambiguous"] is False


def test_eu_column_stays_day_first():
    stats: dict = {}
    assert _dates(["3/4/2024", "15/03/2024"], stats) == ["2024-04-03", "2024-03-15"]
    assert stats["date_order"] == "dmy"
    assert stats["date_order_ambiguous"] is False


def test_all_ambiguous_slash_column_keeps_day_first_and_says_so():
    stats: dict = {}
    assert _dates(["03/04/2024"], stats) == ["2024-04-03"]
    assert stats["date_order"] == "dmy"
    assert stats["date_order_ambiguous"] is True


def test_twelve_hour_time_after_the_date_is_accepted():
    assert _dates(["3/15/2024 6:30 PM", "3/16/2024"]) == ["2024-03-15", "2024-03-16"]
    assert _dates(["05.03.2024 18:30"]) == ["2024-03-05"]
    assert _parse_row_date("7 Aug 2026, 6:30 PM", TODAY) == dt.date(2026, 8, 7)


def test_dotted_dates_are_never_month_first():
    """Точки — всегда дд.мм, даже рядом с американской колонкой: их формат
    бот не угадывает (см. parser.parse_ru_date)."""
    stats: dict = {}
    assert _dates(["05.03.2024", "04.03.2024"], stats) == ["2024-03-05", "2024-03-04"]
    assert stats["date_order"] is None


# ---------- 5. одно упражнение, разные написания ----------


def test_spelling_variants_group_into_one_exercise():
    rows = [
        ["2024-02-01", "Жим лёжа", "80", "8"],
        ["2024-02-01", "жим лежа", "80", "8"],
        ["2024-02-01", "Жим лёжа ", "80", "8"],
    ]
    (workout,) = _build_workout_groups(rows, MAPPING, today=TODAY)
    assert [e["name"] for e in workout["entries"]] == ["Жим лёжа"]
    assert len(workout["entries"][0]["sets"]) == 3


# ---------- 8. UTC+13/+14 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", [-11, 0, 3, 12, 13, 14])
async def test_backdated_started_at_stays_on_the_file_date(fresh_db, tz):
    db = fresh_db
    await db.get_or_create_user(telegram_id=111, username="t")
    await db.update_user(111, tz_offset=tz)
    ex_id = await db.create_exercise(111, "Жим", None)

    imported, failed = await csv_import.apply_import(
        111, [{"date": "2026-03-14", "entries": [{"name": "Жим", "sets": [[80.0, 8, None]]}]}],
        {"Жим": ex_id},
    )

    assert (imported, failed) == (1, 0)
    (workout,) = await db.list_workouts(111, limit=5, offset=0, status="finished")
    started = workout["started_at"]
    assert started == timeutil.backdated_moment(dt.date(2026, 3, 14), tz)
    # И сырая дата строки, и местный день атлета — ровно день из файла.
    assert started[:10] == "2026-03-14"
    local = dt.datetime.fromisoformat(started) + dt.timedelta(hours=tz)
    assert local.date() == dt.date(2026, 3, 14)


# ---------- 7. пачки к модели и точное совпадение с каталогом ----------


@pytest.mark.asyncio
async def test_alias_matching_is_batched(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    batches: list[list[str]] = []

    async def create(**kwargs):
        names = json.loads(kwargs["messages"][1]["content"])["import_names"]
        batches.append(names)
        content = json.dumps({"matches": [{"import_name": names[0], "catalog_name": "Жим штанги лёжа"}]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    monkeypatch.setattr(ai_trainer, "_log_llm_cost", AsyncMock())
    names = [f"Exercise {i}" for i in range(95)]

    result = await ai_trainer.match_exercise_names_to_catalog(1, names)

    assert [len(b) for b in batches] == [40, 40, 15]
    assert sum(batches, []) == names
    # Ответ каждой пачки учтён, а не только последней.
    assert set(result) == {"Exercise 0", "Exercise 40", "Exercise 80"}


@pytest.mark.asyncio
async def test_exact_catalog_names_resolve_without_the_model(fresh_db, user_id, monkeypatch):
    """Модель выключена — «жим штанги лежа» всё равно ложится упражнением
    каталога с его группой мышц, а не голым именем без группы."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    db = fresh_db

    resolved = await csv_import.resolve_exercise_names_via_ai(
        user_id, ["жим штанги лежа", "Что-то своё"]
    )

    assert set(resolved) == {"жим штанги лежа"}
    ex = await db.get_exercise(resolved["жим штанги лежа"])
    assert ex["primary_group_id"] is not None
    assert ex["original_name"] == "Жим штанги лёжа"


# ---------- REST: согласие на AI, дубли, поля превью ----------


async def _client(db, telegram_id=111, consent=False):
    await db.get_or_create_user(telegram_id=telegram_id, username="t")
    if consent:
        await db.update_user(telegram_id, ai_consent_at=db.now_iso())
    code = await db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


CONSENT_HEADERS = {"X-AI-Consent-Flow": "1"}
HEVY_CSV = (
    "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
    "set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n"
    'A,"7 Aug 2026, 08:27",,,Bench Press (Barbell),,,0,warmup,40,12,,,\n'
    'A,"7 Aug 2026, 08:27",,,Bench Press (Barbell),,,1,normal,100,5,,,\n'
    'A,"7 Aug 2026, 08:27",,,Plank,,,0,normal,,,,60,\n'
    'A,"7 Aug 2026, 08:27",,,Жим штанги лёжа,,,0,normal,90,5,,,\n'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", [False, True])
async def test_import_without_ai_consent_skips_the_model_silently(fresh_db, monkeypatch, consent):
    db = fresh_db
    calls: list[list[str]] = []

    async def fake_match(uid, names):
        calls.append(list(names))
        return {"Bench Press (Barbell)": "Жим штанги лёжа"}

    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)
    client = await _client(db, consent=consent)

    resp = await client.post("/import/csv", json={"csv": HEVY_CSV}, headers=CONSENT_HEADERS)

    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 1
    # Точное имя каталога — без модели в любом случае, и с группой мышц.
    catalog = await db.find_exercise_by_name(111, "Жим штанги лёжа")
    assert catalog["primary_group_id"] is not None
    bench = await db.find_exercise_by_name(111, "Bench Press (Barbell)")
    if consent:
        assert calls == [["Bench Press (Barbell)"]]
        assert bench["original_name"] == "Жим штанги лёжа"
    else:
        assert calls == []
        # Заведено как есть, под именем из файла, — тренировка не потеряна.
        assert bench is not None
        assert bench["primary_group_id"] is None


@pytest.mark.asyncio
async def test_rest_import_merges_spelling_variants(fresh_db, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    db = fresh_db
    client = await _client(db)
    text = "date,exercise,weight,reps\n2024-02-01,Жим лёжа,80,8\n2024-02-01,жим лежа,80,8\n2024-02-01,Жим лёжа ,80,8\n"

    preview = (await client.post("/import/csv/preview", json={"csv": text})).json()
    assert [e["name"] for e in preview["exercises"]] == ["Жим лёжа"]
    resp = await client.post("/import/csv", json={"csv": text})

    assert resp.json()["sets_imported"] == 3
    cur = await db.conn().execute(
        "SELECT COUNT(*) FROM exercises WHERE user_id = 111 AND is_template = 0"
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_preview_reports_skipped_rows_and_date_order(fresh_db, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _client(fresh_db)

    body = (await client.post("/import/csv/preview", json={"csv": HEVY_CSV})).json()

    # Прежние поля на месте — их декодирует приложение.
    assert {"workout_count", "set_count", "date_range", "exercises", "duplicate_dates"} <= body.keys()
    assert body["set_count"] == 2
    assert body["rows_skipped"] == 2
    assert body["rows_skipped_warmup"] == 1
    assert body["rows_skipped_no_load"] == 1
    assert body["date_order"] is None
    assert body["date_order_ambiguous"] is False

    ambiguous = "date,exercise,weight,reps\n03/04/2024,Bench Press,100,5\n"
    body = (await client.post("/import/csv/preview", json={"csv": ambiguous})).json()
    assert body["date_range"] == {"from": "2024-04-03", "to": "2024-04-03"}
    assert body["date_order"] == "dmy"
    assert body["date_order_ambiguous"] is True


@pytest.mark.asyncio
async def test_rest_rejects_future_iso_date_in_the_users_language(fresh_db):
    db = fresh_db
    client = await _client(db)
    await db.update_user(111, lang="en")
    text = "date,exercise,weight,reps\n2024-02-01,Bench,80,8\n2099-01-01,Bench,80,8\n"

    resp = await client.post("/import/csv/preview", json={"csv": text})

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_csv"
    with i18n.use_lang("en"):
        assert i18n.t("input.date_in_future") in body["message"]
