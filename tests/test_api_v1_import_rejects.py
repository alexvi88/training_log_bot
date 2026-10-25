"""`POST /v1/import/csv` — отказы до любой записи.

Импорт пишет сотни подходов разом, и отказ обязан быть целиком: ни одной
тренировки, ни одного заведённого упражнения, а бронь «импорт уже идёт»
(общий с ботом `_saving`) снята — иначе следующий честный файл получил бы
409 до перезапуска процесса. Здесь — ветки, которые раньше не исполнялись:
выбор человека (`exercise_mapping`) с шаблонным/архивным/кривым id и файлы,
которые не разбираются (пустой, без строк, мало колонок, нет узнаваемых
колонок, нет ни одного подхода, неизвестная единица файла).
"""

import httpx
import pytest

import ai_trainer
import api_v1

pytestmark = pytest.mark.asyncio

USER = 111
GOOD_CSV = "date,exercise,weight,reps\n2024-01-01,Присед,100,5\n"


@pytest.fixture(autouse=True)
def _no_ai_matching(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


async def _client(fresh_db):
    await fresh_db.get_or_create_user(telegram_id=USER, username="tester")
    code = await fresh_db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _counts(db):
    out = []
    for sql in (
        "SELECT COUNT(*) FROM workouts WHERE user_id = ?",
        "SELECT COUNT(*) FROM exercises WHERE user_id = ?",
        "SELECT COUNT(*) FROM sets s JOIN exercises e ON e.id = s.exercise_id WHERE e.user_id = ?",
    ):
        cur = await db.conn().execute(sql, (USER,))
        out.append((await cur.fetchone())[0])
    return tuple(out)


async def _assert_nothing_written_and_unlocked(db, client, before):
    assert await _counts(db) == before
    # Бронь снята: следующий нормальный файл проходит, а не 409.
    resp = await client.post("/import/csv", json={"csv": GOOD_CSV})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workouts_imported"] == 1


async def _template_id(db):
    cur = await db.conn().execute("SELECT id FROM exercises WHERE is_template = 1 LIMIT 1")
    row = await cur.fetchone()
    assert row is not None, "каталог шаблонов должен быть засеян init_db"
    return row[0]


@pytest.mark.parametrize("kind", ["template", "archived", "bool", "string", "missing"])
async def test_mapping_with_unusable_exercise_id_is_rejected_whole(fresh_db, kind):
    db = fresh_db
    client = await _client(db)
    gid = await db.create_muscle_group(USER, "Ноги")
    if kind == "template":
        ex_id = await _template_id(db)
    elif kind == "archived":
        ex_id = await db.create_exercise(USER, "Старый присед", gid)
        await db.archive_exercise(ex_id)
    elif kind == "bool":
        ex_id = True  # bool в Python — подкласс int, id=1 проскочил бы
    elif kind == "string":
        ex_id = "1"
    else:
        ex_id = 987654
    before = await _counts(db)

    resp = await client.post("/import/csv", json={"csv": GOOD_CSV, "exercise_mapping": {"Присед": ex_id}})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_exercise_mapping"
    await _assert_nothing_written_and_unlocked(db, client, before)


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param([{"exercise_id": 1}], id="list-item-without-name"),
        pytest.param(["Присед"], id="list-item-not-object"),
        pytest.param(5, id="not-object-or-list"),
        pytest.param([{"name": 5, "exercise_id": None}], id="name-not-string"),
    ],
)
async def test_malformed_mapping_is_400_not_500(fresh_db, mapping):
    db = fresh_db
    client = await _client(db)
    before = await _counts(db)

    resp = await client.post("/import/csv", json={"csv": GOOD_CSV, "exercise_mapping": mapping})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] in ("bad_request", "invalid_exercise_mapping")
    await _assert_nothing_written_and_unlocked(db, client, before)


@pytest.mark.parametrize(
    ("csv_text", "error"),
    [
        pytest.param("   \n  \n", "bad_request", id="blank"),
        pytest.param("date,exercise,weight,reps\n", "bad_request", id="header-only"),
        pytest.param("date,exercise\n2024-01-01,Присед\n", "too_few_columns", id="two-columns"),
        pytest.param("foo,bar,baz,qux\n2024-01-01,Присед,100,5\n", "unrecognized_columns", id="unknown-headers"),
        pytest.param("date,exercise,weight,reps\n2024-01-01,Присед,100,0\n", "invalid_csv", id="bad-row"),
        pytest.param(
            "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
            "set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n"
            'A,"7 Aug 2024, 08:27",,,Bench Press (Barbell),,,0,warmup,40,12,,,\n',
            "no_sets_found", id="warmups-only",
        ),
    ],
)
async def test_unparseable_file_writes_nothing(fresh_db, csv_text, error):
    db = fresh_db
    client = await _client(db)
    before = await _counts(db)

    resp = await client.post("/import/csv", json={"csv": csv_text})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == error, resp.text
    # Человеческий текст есть всегда — приложение показывает его как есть,
    # а машинный `detail` — по-английски (его собирает повтор в en-локали).
    assert resp.json().get("message")
    assert not any("а" <= c.lower() <= "я" for c in resp.json()["detail"])
    await _assert_nothing_written_and_unlocked(db, client, before)


async def test_preview_rejects_the_same_files(fresh_db):
    client = await _client(fresh_db)
    resp = await client.post("/import/csv/preview", json={"csv": "date,exercise\n2024-01-01,Присед\n"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "too_few_columns"


@pytest.mark.parametrize("endpoint", ["/import/csv", "/import/csv/preview"])
async def test_unknown_file_unit_is_rejected(fresh_db, endpoint):
    db = fresh_db
    client = await _client(db)
    before = await _counts(db)

    resp = await client.post(endpoint, json={"csv": GOOD_CSV, "file_unit": "stone"})

    assert resp.status_code == 400, resp.text
    assert await _counts(db) == before
