"""Упражнения своим весом без доп. веса — «только повторы» (seed_data.is_reps_only):
вес не вводится, подход с весом отвергается подсказкой про вариант «с весом»,
нагрузку даёт вес тела, а вес тела бот и /v1 спрашивают сами
(db.bodyweight_prompt_for)."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import api_v1
import formatting
import i18n
import parser
import seed_data
from fsm import WorkoutFlow
from handlers import workout

# ---------- признак ----------

def test_reps_only_follows_catalog_identity_not_shown_name():
    assert seed_data.is_reps_only("Подтягивания", "full")
    assert seed_data.is_reps_only("Отжимания на брусьях", "full")
    assert seed_data.is_reps_only(None, "full")  # своё упражнение с галкой
    assert not seed_data.is_reps_only("Подтягивания с весом", "full")
    assert not seed_data.is_reps_only("Жим штанги лёжа", "none")
    assert not seed_data.is_reps_only("Pull-Ups", "none")
    # Всё, что каталог размечает своим весом, кроме «… с весом», — только повторы.
    for name in seed_data.BODYWEIGHT_TEMPLATES:
        expected = name not in seed_data.WEIGHTED_BODYWEIGHT_TEMPLATES
        assert seed_data.is_reps_only(name, "full") is expected, name
    for name in seed_data.WEIGHTED_BODYWEIGHT_TEMPLATES:
        assert name in seed_data.BODYWEIGHT_TEMPLATES


def test_weighted_variant_named_in_athletes_language():
    assert seed_data.weighted_variant_for("подтягивания обратным хватом") == "Подтягивания с весом"
    assert seed_data.weighted_variant_for("Отжимания на брусьях") is None
    ru = seed_data.reps_only_weight_refusal("Подтягивания", "ru")
    en = seed_data.reps_only_weight_refusal("Подтягивания", "en")
    assert "«Подтягивания с весом»" in ru
    assert '"Weighted Pull-Ups"' in en
    assert "с весом" in seed_data.reps_only_weight_refusal("Отжимания на брусьях", "ru")


def test_as_reps_only():
    assert [(p.weight, p.reps) for p in parser.as_reps_only(parser.parse_sets_line("7"))] == [(0.0, 7)]
    assert parser.as_reps_only(parser.parse_sets_line("0 7"))[0].weight == 0.0
    assert parser.as_reps_only(parser.parse_sets_line("60 8")) is None


def test_bodyweight_set_shows_reps_without_zero():
    assert formatting.format_set(0, 7) == "×7"
    assert formatting.format_set(0.0, 7, 9) == "×7 @9"
    assert formatting.format_set(60, 8) == "60×8"


def test_image_footer_drops_zero_tonnage():
    bodyweight = formatting.ExerciseBlockView(group_name="Спина", exercise_name="Подтягивания", sets=[(0, 8)])
    with i18n.use_lang("ru"):
        _t, body, footer, _n = formatting.build_workout_card(dt.datetime(2026, 10, 1), [bodyweight])
        assert "  ×8" in body
        assert "кг" not in footer and "1 подход" in footer
        loaded = formatting.ExerciseBlockView(
            group_name="Спина", exercise_name="Подтягивания", sets=[(0, 8)], set_loads=[80.0]
        )
        _t, _b, footer, _n = formatting.build_workout_card(dt.datetime(2026, 10, 1), [loaded])
        assert footer.endswith("640кг")


# ---------- /v1 ----------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _linked(fresh_db, lang="ru", telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    await fresh_db.set_user_lang(telegram_id, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = _client()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def _fork(client, original_name: str) -> int:
    found = (await client.get("/exercise-templates", params={"query": original_name.split()[0]})).json()
    tpl = next(t for t in found if t["original_name"] == original_name)
    resp = await client.post(f"/exercise-templates/{tpl['id']}/add")
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _set_logged_at(fresh_db, user_id: int, days_ago: int) -> None:
    moment = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).replace(tzinfo=None)
    await fresh_db.conn().execute(
        "UPDATE bodyweight_logs SET logged_at = ? WHERE telegram_id = ?",
        (moment.isoformat(timespec="seconds"), user_id),
    )
    await fresh_db.conn().commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_v1_exercise_carries_reps_only_and_refuses_weight(fresh_db, lang):
    client = await _linked(fresh_db, lang)
    pull = await _fork(client, "Подтягивания")
    weighted = await _fork(client, "Подтягивания с весом")
    dips = await _fork(client, "Отжимания на брусьях")
    bench = await _fork(client, "Жим штанги лёжа")
    by_id = {e["id"]: e for e in (await client.get("/exercises")).json()}
    assert by_id[pull]["reps_only"] is True
    assert by_id[dips]["reps_only"] is True
    assert by_id[weighted]["reps_only"] is False
    assert by_id[bench]["reps_only"] is False

    wid = (await client.post("/workouts/active", json={})).json()["id"]
    resp = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 10, "reps": 8})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "weight_on_bodyweight_exercise"
    variant = "«Подтягивания с весом»" if lang == "ru" else '"Weighted Pull-Ups"'
    assert variant in body["message"]
    resp = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": dips, "weight": 10, "reps": 8})
    assert resp.status_code == 400
    assert "Подтягивания" not in resp.json()["message"]
    resp = await client.post(f"/workouts/{wid}/sets/parse", json={"exercise_id": pull, "text": "10 8"})
    assert resp.status_code == 400 and resp.json()["error"] == "weight_on_bodyweight_exercise"

    # С поясом — вес как у штанги.
    resp = await client.post(f"/workouts/{wid}/sets", json={"exercise_id": weighted, "weight": 10, "reps": 5})
    assert resp.status_code == 201
    # Голые повторы — вес 0, даже если прошлый подход был с весом.
    await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 0, "reps": 8})
    resp = await client.post(f"/workouts/{wid}/sets/parse", json={"exercise_id": pull, "text": "7"})
    assert resp.status_code == 201
    assert [(s["weight"], s["reps"]) for s in resp.json()["sets"]] == [(0, 7)]

    active = (await client.get("/workouts/active")).json()
    flags = {e["exercise_id"]: e["reps_only"] for b in active["blocks"] for e in b["exercises"]}
    assert flags[pull] is True and flags[weighted] is False


@pytest.mark.asyncio
async def test_v1_bodyweight_prompt_missing_then_answer_recounts_load(fresh_db):
    client = await _linked(fresh_db)
    pull = await _fork(client, "Подтягивания")
    bench = await _fork(client, "Жим штанги лёжа")
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    first = (await client.post(f"/workouts/{wid}/sets", json={"exercise_id": bench, "weight": 60, "reps": 5})).json()
    assert first["bodyweight_prompt"] is None  # подходов своим весом ещё нет
    logged = (await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 0, "reps": 8})).json()
    assert logged["bodyweight_prompt"] == {"kind": "missing"}
    assert logged["load_weight"] == 0  # веса тела нет — нагрузка как записано
    assert (await client.get("/workouts/active")).json()["bodyweight_prompt"] == {"kind": "missing"}

    assert (await client.post("/bodyweight", json={"weight": 80})).status_code == 201
    row = await fresh_db.get_set(logged["id"])
    assert row["load_weight"] == 80
    assert (await client.get("/workouts/active")).json()["bodyweight_prompt"] is None
    again = (await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 0, "reps": 6})).json()
    assert again["bodyweight_prompt"] is None and again["load_weight"] == 80


@pytest.mark.asyncio
async def test_v1_bodyweight_prompt_stale_confirm_and_later(fresh_db):
    client = await _linked(fresh_db)
    pull = await _fork(client, "Подтягивания")
    await client.post("/bodyweight", json={"weight": 82})
    await _set_logged_at(fresh_db, 111, 40)
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    logged = (await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 0, "reps": 8})).json()
    prompt = logged["bodyweight_prompt"]
    assert prompt["kind"] == "stale" and prompt["weight"] == 82.0 and prompt["unit"] == "kg"
    assert len(prompt["logged_at"]) == 10
    assert logged["load_weight"] == 82

    # Новое число пересчитывает подходы идущей тренировки.
    await client.post("/bodyweight", json={"weight": 85})
    assert (await fresh_db.get_set(logged["id"]))["load_weight"] == 85
    assert (await client.get("/workouts/active")).json()["bodyweight_prompt"] is None

    # «Позже» — в этой тренировке не спрашивать.
    await _set_logged_at(fresh_db, 111, 40)
    assert (await client.get("/workouts/active")).json()["bodyweight_prompt"]["kind"] == "stale"
    resp = await client.post("/bodyweight/prompt/later")
    assert resp.status_code == 200 and resp.json() == {"bodyweight_prompt": None}
    assert (await client.get("/workouts/active")).json()["bodyweight_prompt"] is None


@pytest.mark.asyncio
async def test_v1_finish_hides_zero_tonnage(fresh_db):
    client = await _linked(fresh_db)
    pull = await _fork(client, "Подтягивания")
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    await client.post(f"/workouts/{wid}/sets", json={"exercise_id": pull, "weight": 0, "reps": 8})
    resp = await client.post(f"/workouts/{wid}/finish", json={})
    assert resp.status_code == 200, resp.text
    rewards = resp.json()
    tonnage = rewards.get("tonnage", rewards.get("rewards", {}).get("tonnage"))
    assert tonnage == ""


@pytest.mark.asyncio
async def test_v1_own_exercise_bodyweight_checkbox(fresh_db):
    client = await _linked(fresh_db)
    resp = await client.post("/exercises", json={"name": "Отжимания на кольцах", "bodyweight": True})
    assert resp.status_code == 201
    own = resp.json()
    assert own["reps_only"] is True and own["bodyweight_load"] == "full"
    plain = (await client.post("/exercises", json={"name": "Тяга чего-то"})).json()
    assert plain["reps_only"] is False
    assert (await client.post("/exercises", json={"name": "X", "bodyweight": "yes"})).status_code == 400

    # Смена галки пересчитывает нагрузку подходов.
    await client.post("/bodyweight", json={"weight": 70})
    wid = (await client.post("/workouts/active", json={})).json()["id"]
    set_row = (await client.post(f"/workouts/{wid}/sets",
                                 json={"exercise_id": plain["id"], "weight": 10, "reps": 5})).json()
    assert set_row["load_weight"] == 10
    patched = (await client.patch(f"/exercises/{plain['id']}", json={"bodyweight": True})).json()
    assert patched["reps_only"] is True
    assert (await fresh_db.get_set(set_row["id"]))["load_weight"] == 80
    patched = (await client.patch(f"/exercises/{plain['id']}", json={"bodyweight": False})).json()
    assert patched["reps_only"] is False
    row = await fresh_db.get_set(set_row["id"])
    assert fresh_db.load_of(row) == 10

    # Каталог: режим задан каталогом — то же значение no-op, другое 400.
    pull = await _fork(client, "Подтягивания")
    assert (await client.patch(f"/exercises/{pull}", json={"bodyweight": True})).status_code == 200
    resp = await client.patch(f"/exercises/{pull}", json={"bodyweight": False})
    assert resp.status_code == 400 and resp.json()["error"] == "bodyweight_not_editable"


# ---------- бот ----------

def _message(user_id: int, text: str, message_id: int = 55):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    msg.text = text
    msg.reply_to_message = None
    msg.delete = AsyncMock()
    msg.reply = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=900))
    msg.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    bot.send_media_group = AsyncMock(return_value=[])
    msg.bot = bot
    return msg


def _callback(user_id: int):
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    cb.answer = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=701, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    bot.send_media_group = AsyncMock(return_value=[])
    cb.bot = bot
    return cb


async def _state(user_id: int, workout_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        open_exercises=[], open_blocks={}, last_by_exercise={}, last_session_sets={},
        weight_steps={}, planned_blocks=[], exercise_targets={},
        live_chat_id=user_id, live_message_id=42, workout_id=workout_id,
    )
    return state


async def _pull_ups(db, user_id: int) -> int:
    group_id = await db.create_muscle_group(user_id, "Спина")
    ex_id = await db.create_exercise(user_id, "Подтягивания", group_id)
    await db.conn().execute("UPDATE exercises SET bodyweight_load = 'full' WHERE id = ?", (ex_id,))
    await db.conn().commit()
    return ex_id


def _text_of(call) -> str:
    return str(call.args[1] if len(call.args) > 1 else call.kwargs.get("text", ""))


def _sent_texts(msg) -> list[str]:
    return [_text_of(c) for c in msg.bot.send_message.call_args_list]


@pytest.mark.asyncio
async def test_bot_bare_number_is_reps_and_weight_is_refused(fresh_db, user_id):
    db = fresh_db
    ex_id = await _pull_ups(db, user_id)
    wid = await db.create_workout(user_id)
    state = await _state(user_id, wid)
    await workout._on_exercise_chosen(_callback(user_id), state, ex_id)

    first = _message(user_id, "7")
    await workout.log_set_text(first, state)
    sets = await db.list_sets_for_workout_exercise(wid, ex_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(0, 7)]
    # Под первым подходом своим весом — вопрос о весе тела, один раз.
    assert any("⚖️" in t for t in _sent_texts(first))
    assert (await db.get_workout(wid))["bodyweight_prompt"] == "shown"

    refused = _message(user_id, "60 8", message_id=56)
    await workout.log_set_text(refused, state)
    assert len(await db.list_sets_for_workout_exercise(wid, ex_id)) == 1
    assert "«Подтягивания с весом»" in refused.reply.call_args.args[0]

    second = _message(user_id, "6", message_id=57)
    await workout.log_set_text(second, state)
    assert not any("⚖️" in t for t in _sent_texts(second))

    # «82 кг» — ответ на вопрос: вес тела записан, нагрузка подходов пересчитана.
    answer = _message(user_id, "82 кг", message_id=58)
    await workout.log_set_text(answer, state)
    assert (await db.get_latest_bodyweight(user_id))["weight"] == 82
    loads = [s["load_weight"] for s in await db.list_sets_for_workout_exercise(wid, ex_id)]
    assert loads == [82, 82]
    assert (await state.get_data())["bw_prompt"] is None


@pytest.mark.asyncio
async def test_bot_stale_weight_same_button(fresh_db, user_id):
    db = fresh_db
    ex_id = await _pull_ups(db, user_id)
    await db.add_bodyweight_log(user_id, 82)
    await _set_logged_at(db, user_id, 45)
    wid = await db.create_workout(user_id)
    state = await _state(user_id, wid)
    await workout._on_exercise_chosen(_callback(user_id), state, ex_id)
    msg = _message(user_id, "8")
    await workout.log_set_text(msg, state)
    prompt_call = next(c for c in msg.bot.send_message.call_args_list if "⚖️" in _text_of(c))
    assert "82кг" in _text_of(prompt_call)
    buttons = prompt_call.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "bwp:same" and buttons[1][0].callback_data == "bwp:later"

    cb = _callback(user_id)
    cb.data = "bwp:same"
    await workout.bodyweight_prompt_answer(cb, state)
    assert await db.count_bodyweight_logs(user_id) == 2
    assert await db.bodyweight_prompt_for(user_id, wid) is None


@pytest.mark.asyncio
async def test_bot_later_marks_workout(fresh_db, user_id):
    db = fresh_db
    ex_id = await _pull_ups(db, user_id)
    wid = await db.create_workout(user_id)
    state = await _state(user_id, wid)
    await workout._on_exercise_chosen(_callback(user_id), state, ex_id)
    await workout.log_set_text(_message(user_id, "8"), state)
    cb = _callback(user_id)
    cb.data = "bwp:later"
    await workout.bodyweight_prompt_answer(cb, state)
    assert (await db.get_workout(wid))["bodyweight_prompt"] == "later"
    assert await db.bodyweight_prompt_for(user_id, wid) is None
    # Следующая тренировка спросит снова.
    await db.finish_workout(wid)
    wid2 = await db.create_workout(user_id)
    block = await db.create_block(wid2, "single")
    await db.add_block_exercise(block, ex_id, 0)
    await db.append_set(block, ex_id, 0, 0, 8)
    assert await db.bodyweight_prompt_for(user_id, wid2) == {"kind": "missing"}


# ---------- «⚖️ Своим весом» у своего упражнения в боте ----------


def _buttons(kb) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def _exm_callback(user_id: int, data: str):
    cb = _callback(user_id)
    cb.data = data
    return cb


async def _exm_state(user_id: int, group_id: int) -> FSMContext:
    from fsm import ExerciseManage

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(ExerciseManage.creating_exercise_name)
    await state.update_data(exm_group_id=group_id)
    return state


@pytest.fixture
def screens(monkeypatch):
    """Перехват ui.safe_edit: что тап по кнопке показал на месте экрана."""
    import ui

    shown: list[tuple[str, object]] = []

    async def _safe_edit(callback, text, reply_markup=None, parse_mode=None, delete=True):
        shown.append((text, reply_markup))

    monkeypatch.setattr(ui, "safe_edit", _safe_edit)
    return shown


@pytest.mark.asyncio
async def test_bot_new_own_exercise_bodyweight_toggle_and_reps_only_sets(fresh_db, user_id, screens):
    """Своё упражнение: на карточке сразу после создания — переключатель
    «⚖️ Своим весом», выключенный. Включил — та же запись, что у галки
    `bodyweight` в /v1 (db.set_exercise_bodyweight), и подход в тренировке —
    только повторы: «12» — это 0×12, «40 8» — отказ."""
    from handlers import exercises

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Спина")
    state = await _exm_state(user_id, group_id)
    msg = _message(user_id, "Подтягивания на кольцах")
    await exercises.exm_new_exercise_name_entered(msg, state)

    ex_id = (await state.get_data())["exm_exercise_id"]
    assert (await db.get_exercise(ex_id))["bodyweight_load"] == "none"
    text = msg.answer.await_args.args[0]
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert "Нажми «⚖️ Своим весом»" in text
    assert _buttons(kb)[0] == ("⚖️ Своим весом: нет", f"exm:bw:{ex_id}:new")

    tap = _exm_callback(user_id, f"exm:bw:{ex_id}:new")
    await exercises.exm_toggle_bodyweight(tap, state)
    ex = await db.get_exercise(ex_id)
    assert (ex["bodyweight_load"], ex["bodyweight_factor"]) == ("full", 1.0)
    assert seed_data.exercise_is_reps_only(ex)
    _, kb = screens[-1]
    assert _buttons(kb)[0] == ("⚖️ Своим весом: да, только повторы", f"exm:bw:{ex_id}:new")
    assert "только повторы" in tap.answer.await_args.args[0]

    wid = await db.create_workout(user_id)
    wstate = await _state(user_id, wid)
    await workout._on_exercise_chosen(_callback(user_id), wstate, ex_id)
    await workout.log_set_text(_message(user_id, "12"), wstate)
    sets = await db.list_sets_for_workout_exercise(wid, ex_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(0, 12)]
    refused = _message(user_id, "40 8", message_id=56)
    await workout.log_set_text(refused, wstate)
    assert len(await db.list_sets_for_workout_exercise(wid, ex_id)) == 1
    assert refused.reply.call_args.args[0] == i18n.t("workout.reps_only_weight")

    # Та же галка в «✏️ Редактировать» — снял, и вес снова принимается.
    text, kb = await exercises._exercise_edit_menu_payload(await db.get_exercise(ex_id))
    assert ("⚖️ Своим весом: да, только повторы", f"exm:bw:{ex_id}:edit") in _buttons(kb)
    off = _exm_callback(user_id, f"exm:bw:{ex_id}:edit")
    await exercises.exm_toggle_bodyweight(off, state)
    assert (await db.get_exercise(ex_id))["bodyweight_load"] == "none"
    _, kb = screens[-1]
    assert ("⚖️ Своим весом: нет", f"exm:bw:{ex_id}:edit") in _buttons(kb)
    assert "✏️ Название" in [t for t, _ in _buttons(kb)]
    await workout.log_set_text(_message(user_id, "40 8", message_id=57), wstate)
    sets = await db.list_sets_for_workout_exercise(wid, ex_id)
    assert [(s["weight"], s["reps"]) for s in sets] == [(0, 12), (40, 8)]


@pytest.mark.asyncio
async def test_bot_bodyweight_toggle_not_offered_for_catalog_copy(fresh_db, user_id, screens):
    """У копии каталога режим задан каталогом — как 400 у PATCH /v1: кнопки
    нет, а старый тап не меняет ничего и объясняет почему."""
    from handlers import exercises

    db = fresh_db
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = 'Подтягивания'"
    )
    ex_id = await db.fork_exercise_from_template(user_id, (await cur.fetchone())["id"])
    _, kb = await exercises._exercise_edit_menu_payload(await db.get_exercise(ex_id))
    assert not any(cb.startswith("exm:bw:") for _, cb in _buttons(kb))

    tap = _exm_callback(user_id, f"exm:bw:{ex_id}:edit")
    await exercises.exm_toggle_bodyweight(tap, await _exm_state(user_id, None))
    assert (await db.get_exercise(ex_id))["bodyweight_load"] == "full"
    assert tap.answer.await_args.kwargs.get("show_alert") is True
    assert not screens


@pytest.mark.asyncio
async def test_bot_bodyweight_toggle_in_english(fresh_db, user_id, screens):
    from handlers import exercises

    db = fresh_db
    await db.set_user_lang(user_id, "en")
    group_id = await db.create_muscle_group(user_id, "Back")
    with i18n.use_lang("en"):
        state = await _exm_state(user_id, group_id)
        msg = _message(user_id, "Ring muscle-up")
        await exercises.exm_new_exercise_name_entered(msg, state)
        ex_id = (await state.get_data())["exm_exercise_id"]
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert _buttons(kb)[0] == ("⚖️ Bodyweight: no", f"exm:bw:{ex_id}:new")
        assert 'Tap "⚖️ Bodyweight"' in msg.answer.await_args.args[0]
        tap = _exm_callback(user_id, f"exm:bw:{ex_id}:new")
        await exercises.exm_toggle_bodyweight(tap, state)
        _, kb = screens[-1]
        assert _buttons(kb)[0][0] == "⚖️ Bodyweight: yes, reps only"
        assert "just log reps" in tap.answer.await_args.args[0]
