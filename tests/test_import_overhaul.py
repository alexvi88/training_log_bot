"""Импорт истории после QA-разбора файла владельца (Hevy): регрессии на каждую
находку H1–H7, M1–M9, L1–L11 — сквозь те же входы, что у живого бота и
приложения (handlers.csv_import, `/v1/import/*`), а не через внутренности.

Файл владельца (OWNER_CSV) — дословно тот, на котором всё это нашлось: четыре
сессии Hevy за 7 августа, разминки, «Warm Up» на время, пустые веса у жима.
Раньше все 217 тестов импорта проходили, а на нём: сессии склеивались в одну
тренировку на 13:30, повторный импорт съедал целый день по «одному общему
упражнению», «Bench Press (Barbell)» раскалывал историю на два упражнения,
отмена оставляла заведённые упражнения, и отменить сам импорт было нельзя.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer
import analytics
import api_v1
import handlers.csv_import as csv_import
import i18n
import i18n_coverage
import keyboards
from fsm import ImportFlow, ResolveFlow
from handlers import exercise_resolve

pytestmark = pytest.mark.asyncio

OWNER_CSV = (
    '"title","start_time","end_time","description","exercise_title","superset_id","exercise_notes",'
    '"set_index","set_type","weight_kg","reps","distance_km","duration_seconds","rpe"\n'
    '"Push","16 Aug 2026, 18:13","16 Aug 2026, 18:18","","Warm Up",,"",0,"normal",,,,300,\n'
    '"Push","16 Aug 2026, 18:13","16 Aug 2026, 18:18","","Bench Press (Barbell)",,"",0,"warmup",50,10,,,\n'
    '"Push","16 Aug 2026, 18:13","16 Aug 2026, 18:18","","Bench Press (Barbell)",,"",1,"warmup",100,4,,,\n'
    '"Push","16 Aug 2026, 18:13","16 Aug 2026, 18:18","","Bench Press (Barbell)",,"",2,"normal",5,10,,,\n'
    '"Вечерняя тренировка 🏋️","10 Aug 2026, 19:21","10 Aug 2026, 19:21","","Bench Press (Barbell)",,"",0,"normal",5,10,,,\n'
    '"Вечерняя тренировка 🏋️","10 Aug 2026, 19:21","10 Aug 2026, 19:21","","Bench Press (Barbell)",,"",1,"normal",5,11,,,\n'
    '"Вечерняя тренировка 🏋️","10 Aug 2026, 19:21","10 Aug 2026, 19:21","","Lat Pulldown (Cable)",,"",0,"normal",50,5,,,\n'
    '"vv","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Bench Press (Barbell)",,"",0,"normal",5,10,,,\n'
    '"vv","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Bench Press (Barbell)",,"",1,"normal",5,11,,,\n'
    '"vv","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Shoulder Press (Dumbbell)",,"",0,"normal",5,55,,,\n'
    '"vv","7 Aug 2026, 08:27","7 Aug 2026, 08:28","","Shoulder Press (Dumbbell)",,"",1,"normal",5,55,,,\n'
    '"Push","7 Aug 2026, 00:52","7 Aug 2026, 00:52","","Bench Press (Barbell)",,"",0,"warmup",50,10,,,\n'
    '"Push","7 Aug 2026, 00:52","7 Aug 2026, 00:52","","Shoulder Press (Dumbbell)",,"",0,"normal",5,50,,,\n'
    '"Pull","7 Aug 2026, 00:51","7 Aug 2026, 00:51","","Lat Pulldown (Cable)",,"",0,"normal",50,5,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",0,"warmup",,10,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",1,"warmup",,4,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Bench Press (Barbell)",,"",2,"normal",,10,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Shoulder Press (Dumbbell)",,"",0,"normal",,12,,,\n'
    '"Push","7 Aug 2026, 00:26","7 Aug 2026, 00:50","","Lateral Raise (Dumbbell)",,"",0,"normal",15,15,,,\n'
)
HEVY_HEADER = (
    "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
    "set_index,set_type,weight_kg,reps,distance_km,duration_seconds,rpe\n"
)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)


# Что ответила бы модель на имена из файла владельца — так, как это было в QA:
# по смыслу «Bench Press (Barbell)» — это «Жим штанги лёжа» каталога.
QA_MODEL = {
    "Bench Press (Barbell)": "Жим штанги лёжа",
    "Lat Pulldown (Cable)": "Тяга верхнего блока",
    "Shoulder Press (Dumbbell)": "Жим гантелей сидя",
    "Lateral Raise (Dumbbell)": "Разведение гантелей в стороны",
}


@pytest.fixture
def qa_model(monkeypatch):
    async def fake_match(user_id, names):
        return {n: QA_MODEL[n] for n in names if n in QA_MODEL}

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "match_exercise_names_to_catalog", fake_match)
    monkeypatch.setattr(ai_trainer, "import_history_overview", AsyncMock(return_value=None))


# ---------- помощники ----------


async def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _message(user_id: int, raw: bytes, name: str = "workout_data.csv"):
    message = MagicMock()
    message.document = SimpleNamespace(file_name=name)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: raw))
    return message


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.text = "экран"
    message.photo = None
    message.edit_text = AsyncMock(return_value=True)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=user_id)))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=1000))
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.answer = AsyncMock()
    callback.message = message
    callback.data = data
    callback.bot = MagicMock()
    callback.bot.send_message = AsyncMock()
    return callback


def _screen(callback):
    """(текст, клавиатура) последнего экрана: правкой сообщения или новым
    сообщением — как решит ui.safe_edit."""
    for mock in (callback.message.edit_text, callback.message.answer):
        if mock.await_args is not None:
            return mock.await_args.args[0], mock.await_args.kwargs.get("reply_markup")
    raise AssertionError("экран не показан")


async def _bot_file(user_id: int, raw: str) -> FSMContext:
    state = await _state(user_id)
    await state.update_data(import_origin="settings")
    await csv_import.import_file_received(_message(user_id, raw.encode()), state)
    return state


async def _bot_save(user_id: int, state: FSMContext, data: str = "imp:save"):
    callback = _callback(user_id, data)
    await csv_import.import_save(callback, state)
    return callback


async def _q(db, sql: str, *params):
    cur = await db.conn().execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def _fork(db, user_id: int, template_name: str) -> int:
    row = (await _q(db, "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", template_name))[0]
    return await db.fork_exercise_from_template(user_id, row["id"])


async def _log(db, user_id: int, exercise_id: int, started_at: str, sets) -> int:
    workout_id = await db.create_finished_workout(user_id, started_at, started_at)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    for i, (w, r) in enumerate(sets, start=1):
        await db.add_set(block_id, exercise_id, i, 0, w, r, None)
    return workout_id


async def _client(db, telegram_id: int = 111, lang: str | None = None) -> httpx.AsyncClient:
    await db.get_or_create_user(telegram_id=telegram_id, username="tester")
    if lang:
        await db.set_user_lang(telegram_id, lang)
    code = await db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


# ---------- H1: сессии, а не дни ----------


async def test_h1_owner_file_keeps_every_hevy_session_with_title_and_real_time(fresh_db, user_id):
    db = fresh_db
    state = await _bot_file(user_id, OWNER_CSV)
    assert await state.get_state() == ImportFlow.confirming
    await _bot_save(user_id, state)

    rows = await _q(db, "SELECT title, started_at, finished_at FROM workouts WHERE user_id = ? ORDER BY started_at", user_id)
    # Шесть сессий Hevy — шесть тренировок, четыре из них 7 августа.
    assert len(rows) == 6
    # Время — настоящее (местное 08:27 у атлета с UTC+3 = 05:27 UTC), а не 13:30.
    vv = next(r for r in rows if r["title"] == "vv")
    assert vv["started_at"] == "2026-08-07T05:27:00"
    assert vv["finished_at"] == "2026-08-07T05:28:00"
    assert [r["title"] for r in rows if r["started_at"].startswith("2026-08-06")] == ["Push", "Pull", "Push"]
    # История и статистика видят их порознь.
    assert await db.count_workouts(user_id) == 6
    by_day = await db.list_finished_workouts_by_day_in_month(user_id, 2026, 8)
    assert len(by_day["2026-08-07"]) == 4


async def test_h1_history_list_tells_same_day_sessions_apart(fresh_db, user_id):
    from handlers import history

    state = await _bot_file(user_id, OWNER_CSV)
    await _bot_save(user_id, state)
    callback = _callback(user_id, "menu:history")
    await history.show_history_list(callback, await _state(user_id), 0)
    text, kb = _screen(callback)
    labels = [b.text for row in kb.inline_keyboard for b in row if b.callback_data.startswith("hist:item:")]
    # Четыре тренировки 7 августа — четыре разные кнопки, время по часам атлета.
    assert sorted(label for label in labels if label.startswith("07.08.2026")) == [
        "07.08.2026 (пт) 00:26", "07.08.2026 (пт) 00:51", "07.08.2026 (пт) 00:52", "07.08.2026 (пт) 08:27",
    ]
    assert "08:27 · vv" in text


# ---------- H2: дубль — это та же сессия ----------


async def test_h2_second_session_of_the_same_day_is_not_a_duplicate(fresh_db, user_id):
    db = fresh_db
    first = HEVY_HEADER + '"AM","7 Aug 2026, 08:00","7 Aug 2026, 09:00",,Squat (Barbell),,,0,normal,100,5,,,\n'
    both = first + '"PM","7 Aug 2026, 18:00","7 Aug 2026, 19:00",,Squat (Barbell),,,0,normal,110,5,,,\n'
    await _bot_save(user_id, await _bot_file(user_id, first))
    state = await _bot_file(user_id, both)
    assert (await state.get_data())["imp_dup_idx"] == [0]
    await _bot_save(user_id, state)
    assert await db.count_workouts(user_id) == 2


async def test_h2_manual_workout_with_the_same_exercise_does_not_swallow_the_day(fresh_db, user_id):
    db = fresh_db
    squat = await _fork(db, user_id, "Присед со штангой")
    await _log(db, user_id, squat, "2026-08-07T15:00:00", [(60.0, 10)])
    raw = HEVY_HEADER + '"AM","7 Aug 2026, 08:00","7 Aug 2026, 09:00",,Squat (Barbell),,,0,normal,100,5,,,\n'
    state = await _bot_file(user_id, raw)
    assert (await state.get_data())["imp_dup_idx"] == []
    await _bot_save(user_id, state)
    assert await db.count_workouts(user_id) == 2


async def test_h2_same_file_twice_is_not_doubled_via_rest(fresh_db):
    client = await _client(fresh_db)
    first = (await client.post("/import/csv", json={"csv": OWNER_CSV})).json()
    assert first["workouts_imported"] == 6
    preview = (await client.post("/import/csv/preview", json={"csv": OWNER_CSV})).json()
    assert preview["all_duplicates"] is True and preview["new_workouts"] == 0
    assert all(w["duplicate"] for w in preview["workouts"])
    second = (await client.post("/import/csv", json={"csv": OWNER_CSV})).json()
    assert second["workouts_imported"] == 0 and second["workouts_skipped_duplicate"] == 6


# ---------- H3/H4/M6: один резолвер, одна идентичность ----------


async def test_h3_hevy_name_goes_into_own_catalog_exercise_in_the_bot(fresh_db, user_id, qa_model):
    db = fresh_db
    bench = await _fork(db, user_id, "Жим штанги лёжа")
    await _log(db, user_id, bench, "2026-07-01T12:00:00", [(70.0, 5)])
    state = await _bot_file(user_id, OWNER_CSV)
    assert await state.get_state() == ImportFlow.confirming
    await _bot_save(user_id, state)
    assert await db.count_workouts(user_id) == 7
    benches = await _q(db, "SELECT id FROM exercises WHERE user_id = ? AND original_name = 'Жим штанги лёжа'", user_id)
    assert [r["id"] for r in benches] == [bench], "история не должна раскалываться на два жима"
    assert await db.find_exercise_by_name(user_id, "Bench Press (Barbell)") is None


async def test_h3_rest_preview_and_commit_use_the_same_identity(fresh_db):
    db = fresh_db
    client = await _client(db)
    bench = await _fork(db, 111, "Жим штанги лёжа")
    preview = (await client.post("/import/csv/preview", json={"csv": OWNER_CSV})).json()
    entry = next(e for e in preview["exercises"] if e["name"] == "Bench Press (Barbell)")
    assert entry == {"name": "Bench Press (Barbell)", "status": "existing", "exercise_id": bench, "needs_choice": False}
    await client.post("/import/csv", json={"csv": OWNER_CSV})
    assert len(await _q(db, "SELECT id FROM exercises WHERE user_id = 111 AND original_name = 'Жим штанги лёжа'")) == 1


async def test_h3_barbell_dumbbell_incline_smith_stay_distinct(fresh_db, user_id):
    db = fresh_db
    await _fork(db, user_id, "Жим штанги лёжа")
    names = ["Bench Press (Dumbbell)", "Incline Bench Press (Barbell)", "Bench Press (Smith Machine)"]
    matches = await csv_import.match_exercise_names(user_id, names)
    for name in names:
        assert matches[name].exercise_id is None, name
        assert matches[name].template_name not in (None, "Жим штанги лёжа"), name


async def test_h3_uncertain_match_asks_in_the_bot_and_creates_nothing(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа широким", gid)
    await db.create_exercise(user_id, "Жим лёжа узким", gid)
    state = await _bot_file(user_id, "date,exercise,weight,reps\n2026-09-01,Жим лёжа,80,5\n")
    assert await state.get_state() == ResolveFlow.picking
    assert len(await db.list_user_exercises(user_id)) == 2


# ---------- B: имена из файла закрепляются за упражнением ----------


async def test_b_choice_is_remembered_and_merge_does_not_resurrect_the_old_name(fresh_db):
    db = fresh_db
    client = await _client(db)
    bench = await _fork(db, 111, "Жим штанги лёжа")
    csv1 = "date,exercise,weight,reps\n2026-09-01,Жим лёжа,80,5\n"
    preview = (await client.post("/import/csv/preview", json={"csv": csv1})).json()
    (item,) = preview["unrecognized_exercises"]
    assert item["needs_choice"] is True and item["suggested_exercise_id"] is None
    await client.post("/import/csv", json={"csv": csv1, "exercise_mapping": [{"name": "Жим лёжа", "exercise_id": bench}]})
    again = (await client.post("/import/csv/preview", json={"csv": csv1.replace("09-01", "09-02")})).json()
    assert again["unrecognized_exercises"] == []
    assert again["exercises"][0]["exercise_id"] == bench

    # Ручное объединение: старое имя → оставшееся упражнение.
    gid = (await db.get_exercise(bench))["primary_group_id"]
    old = await db.create_exercise(111, "Bench Press Old", gid)
    await _log(db, 111, old, "2026-06-01T12:00:00", [(60.0, 5)])
    merge = (await client.post("/exercises/merge", json={"source_id": old, "target_id": bench})).json()
    assert merge["merge_id"]
    await client.post("/import/csv", json={"csv": "date,exercise,weight,reps\n2026-09-03,Bench Press Old,65,5\n"})
    assert await db.find_exercise_by_name(111, "Bench Press Old") is None
    # 80×5 «Жим лёжа» + 60×5 и 65×5 «Bench Press Old» — все в одном жиме.
    assert len(await db.list_sets_for_exercise(bench)) == 3


# ---------- H6: до «Загрузить» в базе ничего ----------


async def test_h6_cancel_at_confirmation_leaves_no_exercises(fresh_db, user_id, qa_model):
    db = fresh_db
    state = await _bot_file(user_id, OWNER_CSV)
    assert await state.get_state() == ImportFlow.confirming
    assert await db.list_user_exercises(user_id) == []
    callback = _callback(user_id, "imp:cancel")
    import handlers.settings as settings
    settings_show = AsyncMock()
    original = settings.show_settings
    settings.show_settings = settings_show
    try:
        await csv_import.import_cancel(callback, state)
    finally:
        settings.show_settings = original
    assert await db.list_user_exercises(user_id) == []
    assert await _q(db, "SELECT id FROM muscle_groups WHERE user_id = ?", user_id) == []


async def test_h6_choices_in_the_resolve_flow_write_nothing(fresh_db, user_id, monkeypatch):
    db = fresh_db
    state = await _state(user_id)
    monkeypatch.setattr(exercise_resolve, "_dispatch_done", AsyncMock())
    await state.update_data(resolve_pending=["A", "B", "C"], resolve_decisions={}, resolve_total=3, resolve_current_name="A")
    await state.set_state(ResolveFlow.picking_new_group)
    groups = await db.list_muscle_groups(user_id)
    await exercise_resolve.resolve_pick_group(_callback(user_id, f"resolvegrp:grp:{groups[0]['id']}"), state)
    await exercise_resolve.resolve_create_all(_callback(user_id, "resolve:createall"), state)
    assert await db.list_user_exercises(user_id) == []
    assert sorted((await state.get_data())["resolve_decisions"]) == ["A", "B", "C"]


# ---------- H7: отмена импорта и объединения ----------


async def test_h7_undo_import_removes_exactly_that_batch(fresh_db):
    db = fresh_db
    client = await _client(db)
    bench = await _fork(db, 111, "Жим штанги лёжа")
    mine = await _log(db, 111, bench, "2026-07-01T12:00:00", [(70.0, 5)])
    result = (await client.post("/import/csv", json={"csv": OWNER_CSV})).json()
    assert result["batch_id"] and result["achievements_unlocked"] >= 1
    batches = (await client.get("/import/batches")).json()["batches"]
    assert [(b["batch_id"], b["source"], b["workouts"], b["can_undo"]) for b in batches] == [
        (result["batch_id"], "hevy", 6, True)
    ]

    undo = (await client.post(f"/import/batches/{result['batch_id']}/undo")).json()
    assert undo["removed_workouts"] == 6 and undo["removed_sets"] == result["sets_imported"]
    # Свой жим с историей остался, заведённые импортом без других подходов — нет.
    assert undo["removed_exercises"] == 3
    assert [w["id"] for w in await db.list_workouts(111, limit=10)] == [mine]
    assert await db.get_exercise(bench) is not None
    assert "Убрал импорт" in undo["message"]
    assert (await client.post(f"/import/batches/{result['batch_id']}/undo")).status_code == 404
    assert (await client.get("/import/batches")).json()["batches"][0]["can_undo"] is False
    tagged = await _q(db, "SELECT code FROM achievements WHERE user_id = 111 AND import_batch_id IS NOT NULL")
    assert tagged == []


async def test_h7_bot_result_message_offers_undo_and_it_works(fresh_db, user_id):
    db = fresh_db
    state = await _bot_file(user_id, OWNER_CSV)
    callback = await _bot_save(user_id, state)
    sent = [c for c in callback.message.answer.await_args_list if c.kwargs.get("reply_markup") is not None]
    kb = sent[-1].kwargs["reply_markup"]
    undo_cb = next(b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data.startswith("imp:undo:"))
    batch_id = undo_cb.split(":", 2)[2]
    go = _callback(user_id, f"imp:undoyes:{batch_id}")
    await csv_import.import_undo_go(go, await _state(user_id))
    assert await db.count_workouts(user_id) == 0
    assert await db.list_user_exercises(user_id) == []


async def test_h7_unmerge_brings_the_exercise_and_its_sets_back(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    keep = await db.create_exercise(user_id, "Жим", gid)
    drop = await db.create_exercise(user_id, "Жим лёжа", gid)
    await db.set_exercise_description(drop, "свой текст")
    await _log(db, user_id, keep, "2026-07-01T12:00:00", [(80.0, 5)])
    await _log(db, user_id, drop, "2026-07-02T12:00:00", [(82.5, 5), (82.5, 4)])
    journal: dict = {}
    assert await db.merge_exercises(user_id, keep, drop, journal_out=journal) == db.MERGE_OK
    assert (await db.get_exercise(keep))["description"] == "свой текст"

    outcome, restored, moved = await db.undo_exercise_merge(user_id, journal["merge_id"])
    assert (outcome, restored, moved) == (db.UNMERGE_OK, drop, 2)
    assert (await db.get_exercise(drop))["display_name"] == "Жим лёжа"
    assert len(await db.list_sets_for_exercise(drop)) == 2
    assert len(await db.list_sets_for_exercise(keep)) == 1
    assert (await db.get_exercise(keep))["description"] is None
    # Имя снова принадлежит своему упражнению — и второго «разъединения» нет.
    assert (await db.find_exercise_by_alias(user_id, "Жим лёжа")) is None
    assert (await db.undo_exercise_merge(user_id, journal["merge_id"]))[0] == db.UNMERGE_NOT_FOUND


# ---------- H5: месяцы словом на любом языке ----------


@pytest.mark.parametrize("text, month", [
    *[(f"4 {m} 2026, 19:00", n) for n, m in enumerate(
        ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."], 1)],
    *[(f"4 {m} 2026", n) for n, m in enumerate(
        ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
         "ноября", "декабря"], 1)],
    *[(f"4 {m} 2026", n) for n, m in enumerate(
        ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"], 1)],
    *[(f"4 {m} 2026, 08:27", n) for n, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)],
    *[(f"4 {m} 2026", n) for n, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August", "Sept.", "October",
         "November", "December"], 1)],
    ("Sep 4, 2026, 6:30 PM", 9),
    ("4 нояб. 2025 г., 10:00", 11),
])
async def test_h5_month_names_in_every_hevy_locale(text, month):
    parsed = csv_import._parse_row_date_raw(text)
    assert (parsed.day, parsed.month) == (4, month)


async def test_h5_russian_locale_hevy_file_imports(fresh_db):
    client = await _client(fresh_db)
    csv = (
        "title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,"
        "set_index,set_type,weight_lbs,reps,distance_miles,duration_seconds,rpe\n"
        'S,"4 мая 2026, 19:00","4 мая 2026, 20:00",,Squat (Barbell),,,0,normal,225,5,,,\n'
        'S,"8 сент. 2026, 19:00","8 сент. 2026, 20:00",,Squat (Barbell),,,0,normal,235,5,,,\n'
    )
    resp = await client.post("/import/csv/preview", json={"csv": csv})
    assert resp.status_code == 200, resp.text
    assert [w["date"] for w in resp.json()["workouts"]] == ["2026-09-08", "2026-05-04"]


# ---------- M1: английскому атлету — ни слова по-русски ----------


async def test_m1_english_import_screens_end_to_end(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    gid = await db.create_muscle_group(user_id, "Chest")
    await db.create_exercise(user_id, "Bench Press wide", gid)
    await db.create_exercise(user_id, "Bench Press close", gid)
    raw = "date,exercise,weight,reps\n2026-09-01,Bench Press,80,5\n2026-09-03,Bench Press,82.5,5\n"
    with i18n.use_lang("en"):
        state = await _bot_file(user_id, raw)
        assert await state.get_state() == ResolveFlow.picking
        templates = await db.search_exercise_templates(user_id, "Bench Press")
        kb = keyboards.exercise_resolve_keyboard([], "Bench Press", "resolve", remaining=1, templates=templates)
        tpl_texts = [b.text for row in kb.inline_keyboard for b in row if b.callback_data.startswith("resolve:tpl:")]
        assert tpl_texts and not any(i18n_coverage.has_cyrillic(t) for t in tpl_texts)
        bulk = _callback(user_id, "resolve:createall")
        await state.update_data(resolve_current_name="Bench Press")
        await exercise_resolve.resolve_create_all(bulk, state)
        toast = bulk.answer.await_args_list[0].args[0]
        assert "Другое" not in toast and "other" in toast.lower()
        callback = await _bot_save(user_id, state)
        result_texts = [c.args[0] for c in callback.message.answer.await_args_list if c.args]
        assert result_texts and all("Загрузил" not in t for t in result_texts)
        overview = await ai_trainer.import_history_overview(user_id, batch_id=(await db.list_import_batches(user_id, 30))[0]["id"])
    assert overview.startswith("Moved over 2 workouts")
    assert not any("Ѐ" <= ch <= "ӿ" for ch in overview)


async def test_m1_short_overview_counts_only_this_import(fresh_db):
    db = fresh_db
    client = await _client(db)
    squat = await _fork(db, 111, "Присед со штангой")
    for day in range(1, 11):
        await _log(db, 111, squat, f"2026-06-{day:02d}T12:00:00", [(100.0, 5)])
    result = (await client.post("/import/csv", json={"csv": "date,exercise,weight,reps\n2026-09-01,Жим лёжа,80,5\n"
                                                             "2026-09-03,Жим лёжа,80,5\n"})).json()
    text = await ai_trainer.import_history_overview(111, batch_id=result["batch_id"])
    assert text.startswith("Перенёс 2 тренировки")


# ---------- M2: пустой вес ----------


async def test_m2_empty_weight_is_not_a_zero_kilo_record(fresh_db):
    db = fresh_db
    client = await _client(db)
    body = (await client.post("/import/csv", json={"csv": OWNER_CSV})).json()
    zero = await _q(db, "SELECT s.id FROM sets s JOIN exercises e ON e.id = s.exercise_id WHERE e.user_id = 111 AND s.weight = 0")
    assert zero == []
    no_weight = next(item for item in body["skipped"] if item["reason"] == "no_weight")
    assert no_weight["count"] == 2


# ---------- M3: e1RM только до 12 повторов ----------


async def test_m3_e1rm_ignores_sets_longer_than_twelve_reps(fresh_db, user_id):
    db = fresh_db
    assert analytics.e1rm(5.0, 55) == 0.0
    assert analytics.e1rm(100.0, 12) > 100.0
    session = analytics.SessionStats(1, "2026-08-07T10:00:00", [analytics.SetRow(5.0, 55), analytics.SetRow(5.0, 50)])
    assert session.top_e1rm == 0.0
    records = analytics.compute_personal_records([session])
    assert records.max_e1rm == 0.0
    ex = await _fork(db, user_id, "Жим гантелей сидя")
    wid = await _log(db, user_id, ex, "2026-08-07T10:00:00", [(5.0, 55)])
    assert await db.max_e1rm_before_workout(user_id, ex, wid + 1) == 0


# ---------- M4: что пропущено и почему; суперсеты ----------


async def test_m4_skipped_rows_are_reported_by_category_and_supersets_kept(fresh_db):
    db = fresh_db
    client = await _client(db)
    csv = HEVY_HEADER + (
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Treadmill,,,0,normal,,,2.5,900,\n'
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Plank,,,0,normal,,,,60,\n'
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Squat (Barbell),,,0,warmup,20,10,,,\n'
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Bicep Curl (Dumbbell),1,,0,normal,12,10,,,\n'
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Triceps Pushdown,1,,0,normal,30,12,,,\n'
        '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Squat (Barbell),,,1,normal,100,abc,,,\n'
    )
    preview = (await client.post("/import/csv/preview", json={"csv": csv})).json()
    reasons = {item["reason"]: item for item in preview["skipped"]}
    assert set(reasons) == {"cardio", "duration_only", "warmup", "bad_line"}
    assert reasons["cardio"]["examples"] == ["Treadmill"]
    assert reasons["bad_line"]["examples"][0].startswith("Строка 7")
    assert all(item["label"] for item in preview["skipped"])
    await client.post("/import/csv", json={"csv": csv})
    blocks = await _q(db, "SELECT b.type, COUNT(be.id) n FROM workout_blocks b JOIN block_exercises be ON be.block_id = b.id GROUP BY b.id")
    assert {"type": "superset", "n": 2} in blocks


# ---------- M7: «с чем объединить» — сразу свой список ----------


async def test_m7_merge_target_list_is_not_empty_before_typing(fresh_db, user_id):
    from handlers import exercises

    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    source = await db.create_exercise(user_id, "Bench Press (Barbell)", gid)
    target = await db.create_exercise(user_id, "Жим штанги лёжа", gid)
    callback = _callback(user_id, f"exm:mergestart:{source}")
    await exercises.exm_merge_start(callback, await _state(user_id))
    _, kb = _screen(callback)
    assert f"exm:mergepick:{target}" in [b.callback_data for row in kb.inline_keyboard for b in row]


# ---------- L: мелочи, которые видно ----------


async def test_l2_bulk_create_label_counts_what_it_creates():
    kb = keyboards.exercise_resolve_keyboard([], "X", "resolve", remaining=3)
    label = next(b.text for row in kb.inline_keyboard for b in row if b.callback_data == "resolve:createall")
    assert label == "➕ Создать все оставшиеся (4)"


async def test_l4_imported_badges_are_dated_by_the_workout_and_summarised(fresh_db, user_id):
    db = fresh_db
    state = await _bot_file(user_id, OWNER_CSV)
    callback = await _bot_save(user_id, state)
    first = (await _q(db, "SELECT earned_at, import_batch_id FROM achievements WHERE user_id = ? AND code = 'first'", user_id))[0]
    assert first["earned_at"].startswith("2026-08-06")
    assert first["import_batch_id"]
    result = [c.args[0] for c in callback.message.answer.await_args_list if c.args and "Загрузил" in c.args[0]]
    assert len(result) == 1 and "🌱" in result[0]


async def test_l5_template_choice_keeps_the_file_name(fresh_db, user_id):
    db = fresh_db
    resolved = await csv_import.materialize_decisions(
        user_id, {"Bench Press (Barbell)": {"kind": "template", "template": "Жим штанги лёжа"}},
        ["Bench Press (Barbell)"],
    )
    ex = await db.get_exercise(resolved["Bench Press (Barbell)"])
    assert ex["display_name"] == "Bench Press (Barbell)"
    assert ex["original_name"] == "Жим штанги лёжа"


async def test_l6_bot_reads_pasted_notes_after_consent(fresh_db, user_id, monkeypatch):
    import text_import

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_extract(uid, text, today, lang=None):
        return text_import.ExtractResult(
            rows=[{"date": "2026-09-12", "exercise": "Становая тяга", "weight": 120.0, "unit": None, "reps": 5,
                   "note": "последний тяжело"}],
            skipped=[{"text": "бег 3км", "reason": "cardio"}],
        )

    monkeypatch.setattr(text_import, "extract_sets", fake_extract)
    state = await _state(user_id)
    await state.set_state(ImportFlow.awaiting_file)
    message = MagicMock()
    message.text = "12.09\nстановая 120х5\nбег 3км"
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.answer = AsyncMock()
    message.reply = AsyncMock()
    await csv_import.import_notes_received(message, state)
    kb = message.answer.await_args.kwargs["reply_markup"]
    assert "imp:notes:go" in [b.callback_data for row in kb.inline_keyboard for b in row]
    callback = _callback(user_id, "imp:notes:go")
    await csv_import.import_notes_go(callback, state)
    assert await state.get_state() == ImportFlow.confirming
    confirm = callback.message.answer.await_args.args[0]
    assert "бег 3км" in confirm
    await _bot_save(user_id, state)
    assert await _q(fresh_db, "SELECT note FROM exercise_notes") == [{"note": "последний тяжело"}]


async def test_l7_name_matching_goes_through_paid_call(fresh_db, monkeypatch):
    import ai_limits

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    calls = []

    async def fake_paid_call(user_id, kind, factory, **kwargs):
        calls.append(kind)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"matches": []}'))])

    monkeypatch.setattr(ai_trainer, "paid_call", fake_paid_call)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: object())
    monkeypatch.setattr(ai_limits, "hard_stop_block", AsyncMock(return_value=None))
    await ai_trainer.match_exercise_names_to_catalog(1, ["Weird Move"])
    assert calls == [None]
    monkeypatch.setattr(ai_limits, "hard_stop_block", AsyncMock(return_value=object()))
    assert await ai_trainer.match_exercise_names_to_catalog(1, ["Weird Move"]) == {}
    assert calls == [None]


async def test_l8_opening_an_imported_workout_does_not_buy_a_comment(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    comment = AsyncMock(return_value="платный текст")
    monkeypatch.setattr(ai_trainer, "comment_on_workout", comment)
    state = await _bot_file(user_id, OWNER_CSV)
    await _bot_save(user_id, state)
    wid = (await fresh_db.list_workouts(user_id, limit=1))[0]["id"]
    user = await fresh_db.get_user(user_id)
    assert await ai_trainer.ensure_workout_comment(user, wid) is None
    comment.assert_not_awaited()


async def test_l10_cancel_all_returns_to_the_main_menu_when_started_there(fresh_db, user_id, monkeypatch):
    shown = AsyncMock()
    monkeypatch.setattr("handlers.workout._show_main_menu", shown)
    state = await _state(user_id)
    await state.update_data(import_origin="menu", resolve_pending=["X"])
    await state.set_state(ResolveFlow.picking)
    await exercise_resolve.resolve_cancel_all(_callback(user_id, "resolve:cancelall"), state)
    shown.assert_awaited()


# ---------- даты, заметки, прогресс, удаление аккаунта ----------


async def test_ambiguous_slash_dates_are_named_in_a_warning(fresh_db):
    client = await _client(fresh_db, lang="en")
    body = (await client.post("/import/csv/preview", json={"csv": "date,exercise,weight,reps\n03/09/2026,Squat,100,5\n"})).json()
    (warning,) = body["warnings"]
    assert warning["code"] == "ambiguous_date"
    assert "03/09/2026" in warning["message"] and "September 3" in warning["message"]


async def test_exercise_notes_from_the_file_are_kept(fresh_db):
    client = await _client(fresh_db)
    csv = HEVY_HEADER + '"A","1 Sep 2026, 18:00","1 Sep 2026, 19:00",,Squat (Barbell),,"последний тяжело",0,normal,100,5,,,\n'
    await client.post("/import/csv", json={"csv": csv})
    assert await _q(fresh_db, "SELECT note FROM exercise_notes") == [{"note": "последний тяжело"}]


async def test_tiny_e1rm_drop_is_flat_not_a_red_arrow(fresh_db):
    db = fresh_db
    client = await _client(db)
    bench = await _fork(db, 111, "Жим штанги лёжа")
    await _log(db, 111, bench, "2026-08-01T12:00:00", [(100.0, 5)])
    await _log(db, 111, bench, "2026-08-08T12:00:00", [(99.9, 5)])
    body = (await client.get(f"/exercises/{bench}/progress/sessions")).json()
    assert body["comparison"]["trend"] == "flat"
    assert body["comparison"]["delta"] == 0.0
    assert "↓" not in body["comparison"]["text"]


async def test_account_deletion_wipes_import_batches_aliases_and_merges(fresh_db):
    import account_deletion

    db = fresh_db
    client = await _client(db)
    bench = await _fork(db, 111, "Жим штанги лёжа")
    await client.post("/import/csv", json={"csv": OWNER_CSV})
    await client.post("/import/csv", json={"csv": "date,exercise,weight,reps\n2026-09-01,Жим лёжа,80,5\n",
                                           "exercise_mapping": [{"name": "Жим лёжа", "exercise_id": bench}]})
    gid = (await db.get_exercise(bench))["primary_group_id"]
    other = await db.create_exercise(111, "Старый жим", gid)
    await db.merge_exercises(111, bench, other)
    for table in ("import_batches", "exercise_aliases", "exercise_merges"):
        assert await _q(db, f"SELECT 1 FROM {table} WHERE user_id = 111")
    assert await account_deletion.delete_account(111) == {}


async def test_old_batches_and_merges_are_pruned(fresh_db, user_id):
    db = fresh_db
    batch = await db.create_import_batch(user_id, "csv")
    await db.conn().execute("UPDATE import_batches SET created_at = ? WHERE id = ?",
                            ((dt.datetime.now() - dt.timedelta(days=40)).isoformat(), batch))
    await db.conn().commit()
    assert await db.prune_old_import_batches(30) == 1
