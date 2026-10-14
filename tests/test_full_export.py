"""Полная выгрузка дневника ZIP-архивом (full_export.py): бот и
`GET /v1/export/archive`.

Главное, что держит этот файл: в архиве есть все разделы, которые стирает
«Удалить аккаунт» (тренировки с заметками и идущая тренировка, вес тела,
программы, свои упражнения, еда), и в него не попадает ни строки чужого
аккаунта.
"""

import csv
import io
import re
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import api_v1
import db
import full_export
import i18n
import keyboards

pytestmark = pytest.mark.asyncio

ME = 111
STRANGER = 222


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id: int):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _fill(user_id: int, tag: str) -> dict:
    """Дневник со всеми разделами; `tag` метит каждую строку владельца."""
    await db.get_or_create_user(telegram_id=user_id, username=f"user{user_id}")
    group_id = await db.create_muscle_group(user_id, f"Группа {tag}")
    ex_id = await db.create_exercise(user_id, f"Жим {tag}", group_id)
    noted_ex = await db.create_exercise(user_id, f"Тяга {tag}", group_id)
    await db.set_exercise_description(ex_id, f"описание {tag}")
    await db.set_exercise_alias(user_id, f"bench {tag}", ex_id, "import")

    wid = await db.create_workout(user_id, started_at="2026-05-01T10:00:00")
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.add_set(block, ex_id, 1, 0, 100.0, 5)
    await db.set_workout_exercise_note(wid, ex_id, f"заметка жима {tag}")
    await db.set_workout_exercise_note(wid, noted_ex, f"заметка без подходов {tag}")
    await db.finish_workout(wid, note=f"заметка тренировки {tag}", finished_at="2026-05-01T11:00:00")

    # Тренировка без подходов, но с заметкой — не теряется.
    empty = await db.create_workout(user_id, started_at="2026-05-02T10:00:00")
    await db.finish_workout(empty, note=f"просто заметка {tag}", finished_at="2026-05-02T10:30:00")

    # Идущая тренировка тоже стирается удалением — значит, тоже в архив.
    active = await db.create_workout(user_id, started_at="2026-05-03T10:00:00")
    ablock = await db.create_block(active, "single")
    await db.add_block_exercise(ablock, ex_id, 0)
    await db.add_set(ablock, ex_id, 1, 0, 60.0, 12)

    await db.add_bodyweight_log(user_id, 81.5, logged_at="2026-05-01T08:00:00")
    pid = await db.create_program(user_id, f"Программа {tag}", description=f"о программе {tag}")
    rid = await db.create_routine(user_id, f"День {tag}", program_id=pid, day_order=0)
    await db.add_routine_exercise(rid, ex_id, 0, target="3×8")
    await db.add_food_entry(user_id, "2026-05-01", f"гречка {tag}", calories=300, protein=10)
    return {"workout": wid, "exercise": ex_id}


def _read(data: bytes) -> dict[str, list[list[str]]]:
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            text = zf.read(name).decode("utf-8-sig")
            out[name] = text if name.endswith(".txt") else list(csv.reader(io.StringIO(text)))
    return out


async def test_archive_has_every_section(fresh_db):
    me = await _fill(ME, "мой")
    coach = 333
    await db.get_or_create_user(telegram_id=coach, username="coach")
    await db.add_coach_comment(coach, ME, me["workout"], "держи спину")

    data, name = await full_export.build_archive_bytes(ME)
    files = _read(data)

    assert re.fullmatch(r"training_log_\d{4}-\d{2}-\d{2}\.zip", name)
    assert set(files) == set(full_export.REQUIRED_FILES) | set(full_export.OPTIONAL_FILES)

    workouts = files["workouts.csv"]
    assert workouts[0] == full_export.WORKOUTS_HEADER
    # Первые шесть колонок — те же, что у старого training_log.csv.
    assert workouts[0][:6] == ["started_at", "exercise", "round_index", "weight", "reps", "rpe"]
    flat = "\n".join(",".join(r) for r in workouts)
    for text in ("заметка тренировки мой", "заметка жима мой", "заметка без подходов мой", "просто заметка мой"):
        assert text in flat
    statuses = {r[8] for r in workouts[1:]}
    assert statuses == {"finished", "active"}
    assert any(r[3] == "60" and r[4] == "12" and r[8] == "active" for r in workouts[1:])

    assert files["bodyweight.csv"][1][1:] == ["81.5", "kg"]
    programs = files["programs.csv"]
    assert programs[1][0] == "Программа мой" and programs[1][6] == "День мой"
    assert programs[1][8] == "Жим мой" and programs[1][9] == "3×8"
    exercises = {r[0]: r for r in files["exercises.csv"][1:]}
    assert exercises["Жим мой"][7] == "описание мой"
    assert "bench мой" in exercises["Жим мой"][8]
    assert files["food.csv"][1][1] == "гречка мой"
    # Комментарий тренера — текстом, без того, кто его написал.
    assert files["coach_comments.csv"][1][2] == "держи спину"
    assert "coach" not in ",".join(files["coach_comments.csv"][0] + files["coach_comments.csv"][1])

    readme = files["README.txt"]
    for name in full_export.REQUIRED_FILES[1:] + full_export.OPTIONAL_FILES:
        assert name in readme


async def test_stranger_data_never_gets_in(fresh_db):
    await _fill(ME, "мой")
    await _fill(STRANGER, "чужой")
    data, _ = await full_export.build_archive_bytes(ME)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        blob = "".join(zf.read(n).decode("utf-8-sig") for n in zf.namelist())
    assert "мой" in blob
    assert "чужой" not in blob


async def test_empty_diary_gives_required_files_only(fresh_db):
    await db.get_or_create_user(telegram_id=ME, username="me")
    data, _ = await full_export.build_archive_bytes(ME)
    files = _read(data)
    assert set(files) == set(full_export.REQUIRED_FILES)
    assert files["workouts.csv"] == [full_export.WORKOUTS_HEADER]
    assert "food.csv" not in files["README.txt"]


async def test_readme_speaks_the_athlete_language(fresh_db):
    await db.get_or_create_user(telegram_id=ME, username="me")
    await db.set_user_lang(ME, "en")
    files = _read((await full_export.build_archive_bytes(ME))[0])
    assert not re.search("[А-Яа-яЁё]", files["README.txt"])
    await db.set_user_lang(ME, "ru")
    files = _read((await full_export.build_archive_bytes(ME))[0])
    assert "дневник" in files["README.txt"]


async def test_formula_cells_are_escaped(fresh_db):
    await db.get_or_create_user(telegram_id=ME, username="me")
    await db.add_food_entry(ME, "2026-05-01", "=HYPERLINK(\"x\")")
    files = _read((await full_export.build_archive_bytes(ME))[0])
    assert files["food.csv"][1][1].startswith("'=")


async def test_v1_archive_endpoint(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory, ME)
    await _fill(ME, "мой")
    await _fill(STRANGER, "чужой")

    resp = await client.get("/export/archive")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert re.search(r'filename="training_log_\d{4}-\d{2}-\d{2}\.zip"', resp.headers["content-disposition"])
    files = _read(resp.content)
    assert "Жим мой" in "".join(",".join(r) for r in files["workouts.csv"])
    assert "чужой" not in resp.content.decode("utf-8", errors="ignore")


async def test_v1_archive_requires_token(fresh_db, client_factory):
    resp = await client_factory().get("/export/archive")
    assert resp.status_code == 401


async def test_delete_keyboard_offers_export_first():
    with i18n.use_lang("ru"):
        kb = keyboards.delete_account_keyboard()
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    assert rows == [["settings:delete:export"], ["settings:deleteyes", "settings:menu"]]
    assert kb.inline_keyboard[0][0].text == "📦 Сначала выгрузить"


async def test_delete_confirm_names_what_goes_into_the_archive():
    for lang in ("ru", "en"):
        text = i18n.t_in(lang, "settings.delete.confirm")
        assert i18n.t_in(lang, "btn.delete_export_first") in text
        assert "CSV" not in text


async def test_bot_export_first_sends_archive_then_confirm_again(fresh_db):
    from handlers import settings as settings_handlers

    await _fill(ME, "мой")
    message = SimpleNamespace(
        answer_document=AsyncMock(), answer=AsyncMock(), edit_reply_markup=AsyncMock(),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=ME), message=message, answer=AsyncMock())

    with i18n.use_lang("ru"):
        await settings_handlers.settings_delete_export_first(callback, state=None)

    doc = message.answer_document.await_args.args[0]
    assert doc.filename.endswith(".zip")
    assert "workouts.csv" in zipfile.ZipFile(io.BytesIO(doc.data)).namelist()
    text = message.answer.await_args.args[0]
    assert text == i18n.t_in("ru", "settings.delete.confirm")
    kb = message.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[1][0].callback_data == "settings:deleteyes"
    message.edit_reply_markup.assert_awaited_once()
