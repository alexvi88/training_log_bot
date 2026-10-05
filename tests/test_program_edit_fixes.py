"""Правка сохранённой программы тренером: повторная правка превью не теряет
`replaces`, а сохранение обновляет дни на месте, не рвя историю тренировок."""

import json

import pytest

import ai_program_actions
import ai_trainer
import i18n
from handlers import ai_trainer as handler
from tests.test_ai_trainer_handler import _make_callback, _make_chat_message, _make_state

TEMPLATE_A = "Жим штанги лёжа"
TEMPLATE_B = "Присед со штангой"


def _tool_input(name: str, days: list[str], replaces: str | None = None) -> dict:
    data = {
        "name": name,
        "description": "Описание.",
        "days": [
            {"name": d, "exercises": [{"name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 8}]}
            for d in days
        ],
    }
    if replaces:
        data["replaces_program"] = replaces
    return data


async def _saved_program(db, user_id: int, name="Сплит", days=("Ноги", "Верх")) -> tuple[int, list[int]]:
    program_id = await db.create_program(user_id, name)
    ids = []
    for day in days:
        rid = await db.create_routine_from_program(
            user_id, day, [TEMPLATE_B], program_id=program_id
        )
        ids.append(rid)
    return program_id, ids


async def _finished_workout(db, user_id: int, routine_id: int, started_at: str) -> None:
    wid = await db.create_workout(user_id, started_at=started_at, routine_id=routine_id)
    await db.finish_workout(wid)


# ---------- 1. replaces переезжает в повторную правку ----------


async def test_second_edit_without_replaces_keeps_it_and_saves_into_same_program(
    fresh_db, user_id, monkeypatch
):
    program_id, _ = await _saved_program(fresh_db, user_id)
    inputs = iter([
        _tool_input("Сплит", ["Ноги", "Верх", "Тяга"], replaces="Сплит"),
        _tool_input("Сплит", ["Ноги", "Верх", "Тяга", "Плечи"]),  # replaces забыт
    ])

    async def fake_ask(uid, question, history, on_program=None, **kwargs):
        await ai_trainer.execute_tool(uid, "propose_program", next(inputs), on_program=on_program)
        return "Поправил."

    monkeypatch.setattr(handler.ai_trainer, "ask", fake_ask)
    state = await _make_state(user_id)
    await state.set_state("AITrainerFlow:chatting")

    await handler.ai_question(_make_chat_message(user_id, "добавь тягу"), state)
    first = (await state.get_data())["ai_program_draft"]
    assert first["replaces"]["id"] == program_id

    await handler.ai_question(_make_chat_message(user_id, "и плечи"), state)
    second = (await state.get_data())["ai_program_draft"]
    assert second["replaces"]["id"] == program_id
    assert second["id"] != first["id"]

    await handler.ai_program_save(_make_callback(user_id, f"ai:prog:save:{second['id']}"), state)

    programs = await fresh_db.list_programs(user_id)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Сплит", 4)]


async def test_carry_over_matches_by_replaced_or_previous_draft_name(fresh_db, user_id):
    program_id, _ = await _saved_program(fresh_db, user_id)
    _, previous = None, {
        "name": "Сплит 4д",
        "replaces": (await ai_trainer._resolve_replaced_program(user_id, "Сплит"))[0],
    }

    same_as_draft = await ai_trainer.carry_over_replaces(user_id, previous, {"name": " сплит 4Д "})
    same_as_saved = await ai_trainer.carry_over_replaces(user_id, previous, {"name": "СПЛИТ"})
    assert same_as_draft["replaces"]["id"] == program_id
    assert same_as_saved["replaces"]["id"] == program_id


async def test_carry_over_skips_other_name_explicit_replaces_and_deleted_program(fresh_db, user_id):
    program_id, _ = await _saved_program(fresh_db, user_id)
    replaces = (await ai_trainer._resolve_replaced_program(user_id, "Сплит"))[0]
    previous = {"name": "Сплит", "replaces": replaces}

    other = await ai_trainer.carry_over_replaces(user_id, previous, {"name": "Фуллбоди"})
    assert not other.get("replaces")

    mine = {"name": "Сплит", "replaces": {"id": 999, "kind": "program", "name": "Сплит"}}
    assert (await ai_trainer.carry_over_replaces(user_id, previous, mine))["replaces"]["id"] == 999

    assert not (await ai_trainer.carry_over_replaces(user_id, None, {"name": "Сплит"})).get("replaces")
    assert not (await ai_trainer.carry_over_replaces(
        user_id, {"name": "Сплит"}, {"name": "Сплит"}
    )).get("replaces")

    await fresh_db.delete_program_by_id(program_id)
    gone = await ai_trainer.carry_over_replaces(user_id, previous, {"name": "Сплит"})
    assert not gone.get("replaces")


# ---------- 2. дни обновляются на месте ----------


async def _draft_for(user_id: int, program_name: str, days: list[str]) -> dict:
    payload, draft = None, None

    async def on_program(d):
        nonlocal draft
        draft = d

    raw = await ai_trainer.execute_tool(
        user_id, "propose_program", _tool_input(program_name, days, replaces=program_name),
        on_program=on_program,
    )
    payload = json.loads(raw)
    assert payload.get("replaces_program") == program_name
    return draft


async def test_edit_keeps_routine_ids_and_history(fresh_db, user_id):
    program_id, (legs, upper) = await _saved_program(fresh_db, user_id)
    await _finished_workout(fresh_db, user_id, legs, "2026-09-01T10:00:00")
    await _finished_workout(fresh_db, user_id, upper, "2026-09-03T10:00:00")

    draft = await _draft_for(user_id, "Сплит", ["Ноги", "Верх"])
    result = await ai_program_actions.finalize_program_save(user_id, draft)
    assert result["replacing"] is True

    days = await fresh_db.list_program_days_by_id(program_id)
    assert [d["id"] for d in days] == [legs, upper]
    # состав обновился: теперь жим, а не присед
    exercises = await fresh_db.list_routine_exercises(legs)
    assert [e["display_name"] for e in exercises] == [TEMPLATE_A]

    history = await fresh_db.program_day_history(program_id)
    assert set(history) == {legs, upper}
    nxt = await fresh_db.next_program_day(program_id)
    assert nxt["id"] == legs  # после «Верх» по кругу снова «Ноги»
    recent = await fresh_db.list_recent_programs(user_id, "2026-08-01")
    assert [r["program_id"] for r in recent] == [program_id]
    adherence = await ai_trainer._program_adherence(user_id)
    sessions = {d["day"]: d["sessions"] for d in adherence["programs"][0]["days"]}
    assert sessions == {"Ноги": 1, "Верх": 1}


async def test_renamed_day_keeps_its_id_when_day_count_is_unchanged(fresh_db, user_id):
    program_id, (legs, upper) = await _saved_program(fresh_db, user_id)
    await _finished_workout(fresh_db, user_id, legs, "2026-09-01T10:00:00")

    draft = await _draft_for(user_id, "Сплит", ["Низ", "Верх"])
    await ai_program_actions.finalize_program_save(user_id, draft)

    days = await fresh_db.list_program_days_by_id(program_id)
    assert [(d["id"], d["name"]) for d in days] == [(legs, "Низ"), (upper, "Верх")]
    assert legs in await fresh_db.program_day_history(program_id)


async def test_added_day_is_created_and_old_ones_stay(fresh_db, user_id):
    program_id, (legs, upper) = await _saved_program(fresh_db, user_id)

    draft = await _draft_for(user_id, "Сплит", ["Ноги", "Верх", "Тяга"])
    await ai_program_actions.finalize_program_save(user_id, draft)

    days = await fresh_db.list_program_days_by_id(program_id)
    assert [d["name"] for d in days] == ["Ноги", "Верх", "Тяга"]
    assert [d["id"] for d in days[:2]] == [legs, upper]
    assert days[2]["id"] not in (legs, upper)


async def test_removed_day_is_deleted_and_matched_by_name_survive(fresh_db, user_id):
    program_id, (legs, upper) = await _saved_program(fresh_db, user_id)
    await _finished_workout(fresh_db, user_id, upper, "2026-09-01T10:00:00")

    draft = await _draft_for(user_id, "Сплит", ["Верх"])
    await ai_program_actions.finalize_program_save(user_id, draft)

    days = await fresh_db.list_program_days_by_id(program_id)
    assert [(d["id"], d["name"]) for d in days] == [(upper, "Верх")]
    assert await fresh_db.get_routine(legs) is None
    assert upper in await fresh_db.program_day_history(program_id)


async def test_failure_midway_rolls_everything_back(fresh_db, user_id, monkeypatch):
    program_id, (legs, upper) = await _saved_program(fresh_db, user_id)
    draft = await _draft_for(user_id, "Сплит", ["Верх"])  # «Ноги» уйдёт — последним шагом

    real = fresh_db.conn()

    class Boom:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            return getattr(self._inner, item)

        async def execute(self, sql, *a, **kw):
            if sql.lstrip().startswith("DELETE FROM routines"):
                raise RuntimeError("boom")
            return await self._inner.execute(sql, *a, **kw)

    monkeypatch.setattr(fresh_db, "conn", lambda: Boom(real))
    with pytest.raises(RuntimeError):
        await ai_program_actions.save_into_existing_program(
            user_id, draft, await fresh_db.get_program(program_id)
        )
    monkeypatch.undo()

    days = await fresh_db.list_program_days_by_id(program_id)
    assert [(d["id"], d["name"]) for d in days] == [(legs, "Ноги"), (upper, "Верх")]
    for rid in (legs, upper):
        assert [e["display_name"] for e in await fresh_db.list_routine_exercises(rid)] == [TEMPLATE_B]


# ---------- 3. «Забрать» под старым превью ----------


async def test_old_preview_button_says_it_is_superseded(fresh_db, user_id):
    state = await _make_state(user_id)
    await state.update_data(ai_program_draft={
        "id": "new1", "name": "X",
        "days": [{"name": "Д", "items": [{"name": TEMPLATE_A, "target": None}]}],
    })
    callback = _make_callback(user_id, "ai:prog:save:old0")

    await handler.ai_program_save(callback, state)

    text = callback.answer.await_args.args[0]
    assert text == i18n.t("ai.screen.program_superseded")
    assert "старая версия" in text
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_missing_draft_keeps_the_generic_gone_text(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "ai:prog:save:old0")
    await handler.ai_program_save(callback, state)
    assert callback.answer.await_args.args[0] == i18n.t("ai.screen.program_gone")


def test_superseded_text_is_in_both_catalogs():
    for lang in ("ru", "en"):
        with open(f"locales/{lang}.json", encoding="utf-8") as fh:
            catalog = json.load(fh)
        assert catalog["ai.screen.program_superseded"].strip()
