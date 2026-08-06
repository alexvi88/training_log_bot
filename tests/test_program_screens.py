"""Экраны программ: что теперь можно сделать с сохранённой программой.

До этого можно было переименовать её, удалить и поделиться — и всё. Добавить
день, переставить дни, вынести день наружу, поменять схему подходов у уже
добавленного упражнения было нельзя вовсе, а подписи кнопок на дне и на самой
программе совпадали при том, что удаляли разное.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import config
from fsm import RoutineFlow
from handlers import routines
from seed_data import PROGRAM_BY_KEY

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str = ""):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id))
    )
    message.delete = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.from_user = SimpleNamespace(id=user_id, username="tester")
    message.text = text
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=user_id))
    )
    message.reply = AsyncMock()
    return message


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


def _rendered(callback):
    """Последний вызов, которым экран реально отрисовался — ui.safe_edit может
    и править сообщение, и слать новое, в зависимости от того, что удалось."""
    calls = [
        c for mock in (callback.message.edit_text, callback.message.answer)
        for c in mock.await_args_list
    ]
    return calls[-1] if calls else None


def _buttons(callback) -> list[tuple[str, str]]:
    call = _rendered(callback)
    kb = call.kwargs.get("reply_markup") if call else None
    if kb is None:
        return []
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def _last_text(callback) -> str:
    call = _rendered(callback)
    return call.args[0] if call and call.args else ""


async def _program(db, user_id, name="PPL", days=("Толкай", "Тяни", "Ноги")):
    program_id = await db.create_program(user_id, name)
    for day in days:
        await db.create_routine(user_id, day, program_id=program_id)
    return program_id


# ---------- каталог: повторное добавление ----------


async def test_adding_a_catalog_program_twice_asks_instead_of_duplicating(fresh_db, user_id):
    """Второй тап (а на подвисшей связи он случается сам) дописывал те же дни в
    программу с тем же именем: PPL превращался в «PPL · 6 дней» из двух
    неразличимых троек."""
    db = fresh_db
    state = await _state(user_id)
    await routines.rt_program_add(_make_callback(user_id, "rt:progadd:ppl"), state)

    callback = _make_callback(user_id, "rt:progadd:ppl")
    await routines.rt_program_add(callback, state)

    days = await db.list_program_days_by_id((await db.list_programs(user_id))[0]["id"])
    assert len(days) == len(PROGRAM_BY_KEY["ppl"]["days"])
    assert "уже есть" in _last_text(callback)
    assert "rt:progadd2:ppl" in [cb for _text, cb in _buttons(callback)]


async def test_adding_a_second_copy_on_purpose_gets_a_free_name(fresh_db, user_id):
    db = fresh_db
    state = await _state(user_id)
    await routines.rt_program_add(_make_callback(user_id, "rt:progadd:ppl"), state)

    await routines.rt_program_add_copy(_make_callback(user_id, "rt:progadd2:ppl"), state)

    names = sorted(p["name"] for p in await db.list_programs(user_id))
    original = PROGRAM_BY_KEY["ppl"]["name"]
    assert names == sorted([original, f"{original} (2)"])


async def test_catalog_add_respects_the_day_budget(fresh_db, user_id):
    """Лимит знал только AI-путь; каталог, импорт и «из тренировки» шли мимо."""
    db = fresh_db
    for i in range(config.MAX_ROUTINES_PER_USER):
        await db.create_routine(user_id, f"День {i}")

    callback = _make_callback(user_id, "rt:progadd:ppl")
    await routines.rt_program_add(callback, await _state(user_id))

    assert await db.count_routines(user_id) == config.MAX_ROUTINES_PER_USER
    assert "не влезет" in callback.answer.await_args.args[0]


# ---------- дни: добавить, скопировать, переставить, вынести ----------


async def test_a_blank_day_can_be_added_to_an_existing_program(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    state = await _state(user_id)

    await routines.rt_day_blank(_make_callback(user_id, f"rt:dayblank:{program_id}"), state)
    assert await state.get_state() == RoutineFlow.naming_day
    await routines.rt_day_named(_make_message(user_id, "Руки"), state)

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == [
        "Толкай", "Тяни", "Ноги", "Руки",
    ]
    assert await state.get_state() is None


async def test_copying_a_day_brings_its_exercises_and_schemes(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await _program(db, user_id, days=("Толкай",))
    source = (await db.list_program_days_by_id(program_id))[0]
    await db.add_routine_exercise(source["id"], ex_id, 0, "4×8")
    state = await _state(user_id)

    await routines.rt_day_copy(_make_callback(user_id, f"rt:daycopy:{source['id']}"), state)
    await routines.rt_day_named(_make_message(user_id, "Толкай 2"), state)

    days = await db.list_program_days_by_id(program_id)
    copied = await db.list_routine_exercises(days[1]["id"])
    assert days[1]["name"] == "Толкай 2"
    assert [(ex["display_name"], ex["target"]) for ex in copied] == [("Жим лёжа", "4×8")]


async def test_days_can_be_moved_with_the_arrows(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    days = await db.list_program_days_by_id(program_id)

    await routines.rt_day_move(
        _make_callback(user_id, f"rt:daymv:{days[2]['id']}:up"), await _state(user_id)
    )

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == [
        "Толкай", "Ноги", "Тяни",
    ]


async def test_a_day_can_be_taken_out_of_its_program(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай", "Тяни"))
    day = (await db.list_program_days_by_id(program_id))[0]

    await routines.rt_day_out(
        _make_callback(user_id, f"rt:dayout:{day['id']}"), await _state(user_id)
    )

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["Тяни"]
    assert [r["name"] for r in await db.list_standalone_routines(user_id)] == ["Толкай"]


async def test_adding_a_day_respects_the_budget(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай",))
    for i in range(config.MAX_ROUTINES_PER_USER - 1):
        await db.create_routine(user_id, f"Лишний {i}")

    callback = _make_callback(user_id, f"rt:dayadd:{program_id}")
    await routines.rt_day_add(callback, await _state(user_id))

    assert "не влезет" in callback.answer.await_args.args[0]


# ---------- «какой сегодня день» ----------


async def test_the_program_screen_leads_with_the_day_whose_turn_it_is(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    days = await db.list_program_days_by_id(program_id)
    wid = await db.create_workout(user_id, started_at="2026-08-01T10:00:00", routine_id=days[0]["id"])
    await db.finish_workout(wid, finished_at="2026-08-01T11:00:00")

    callback = _make_callback(user_id, f"rt:prg:{program_id}")
    await routines.rt_program(callback, await _state(user_id))

    labels = [text for text, _cb in _buttons(callback)]
    assert labels[0] == "▶️ Сегодня: Тяни"
    # И день не дублируется ниже собственной кнопкой.
    assert labels.count("Тяни") == 0


async def test_the_program_screen_says_how_long_ago_each_day_was(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай", "Ноги"))
    days = await db.list_program_days_by_id(program_id)
    wid = await db.create_workout(user_id, started_at="2026-08-01T10:00:00", routine_id=days[0]["id"])
    await db.finish_workout(wid, finished_at="2026-08-01T11:00:00")

    callback = _make_callback(user_id, f"rt:prg:{program_id}")
    await routines.rt_program(callback, await _state(user_id))

    text = _last_text(callback)
    assert "ещё не делал" in text
    assert "1 тренировка по ней" in text


async def test_an_old_program_button_still_opens_the_right_program(fresh_db, user_id):
    """Кнопки rt:pgm:<routine_id> живут в чатах и после релиза — старая ручка
    разрешается в программу, а не открывает чужую."""
    db = fresh_db
    program_id = await _program(db, user_id)
    day = (await db.list_program_days_by_id(program_id))[1]

    callback = _make_callback(user_id, f"rt:pgm:{day['id']}")
    await routines.rt_program_days_legacy(callback, await _state(user_id))

    assert "PPL" in _last_text(callback)


# ---------- подписи: день против программы ----------


async def test_deleting_a_day_says_day_not_program(fresh_db, user_id):
    """«🗑 Удалить программу» на дне сносила один день, а точно так же
    подписанная кнопка этажом выше — всю программу."""
    db = fresh_db
    program_id = await _program(db, user_id)
    day = (await db.list_program_days_by_id(program_id))[0]

    callback = _make_callback(user_id, f"rt:view:{day['id']}")
    await routines.rt_view(callback, await _state(user_id))
    assert ("🗑 Удалить день", f"rt:delask:{day['id']}") in _buttons(callback)

    confirm = _make_callback(user_id, f"rt:delask:{day['id']}")
    await routines.rt_delete_confirm(confirm, await _state(user_id))
    text = _last_text(confirm)
    assert "день «Толкай»" in text and "Остальные дни останутся" in text


async def test_deleting_a_standalone_program_still_says_program(fresh_db, user_id):
    db = fresh_db
    rid = await db.create_routine(user_id, "Своя")

    callback = _make_callback(user_id, f"rt:view:{rid}")
    await routines.rt_view(callback, await _state(user_id))

    assert ("🗑 Удалить программу", f"rt:delask:{rid}") in _buttons(callback)


async def test_deleting_a_day_returns_to_its_program(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    day = (await db.list_program_days_by_id(program_id))[0]

    callback = _make_callback(user_id, f"rt:delyes:{day['id']}")
    await routines.rt_delete(callback, await _state(user_id))

    assert callback.answer.await_args.args[0] == "День удалён"
    assert "PPL" in _last_text(callback)


async def test_renaming_a_day_asks_about_the_day(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    day = (await db.list_program_days_by_id(program_id))[0]

    callback = _make_callback(user_id, f"rt:rename:{day['id']}")
    await routines.rt_rename(callback, await _state(user_id))

    assert "название дня" in _last_text(callback)


# ---------- переименование программы в занятое имя ----------


async def test_renaming_a_program_onto_another_offers_a_merge_instead_of_doing_one(
    fresh_db, user_id
):
    db = fresh_db
    alpha = await _program(db, user_id, name="Альфа", days=("A1", "A2"))
    beta = await _program(db, user_id, name="Бета", days=("B1",))
    state = await _state(user_id)
    await routines.rt_program_rename(_make_callback(user_id, f"rt:pgmrename:{beta}"), state)

    message = _make_message(user_id, "Альфа")
    await routines.rt_program_rename_entered(message, state)

    assert {p["name"]: p["day_count"] for p in await db.list_programs(user_id)} == {
        "Альфа": 2, "Бета": 1,
    }
    kb = message.answer.await_args.kwargs["reply_markup"]
    assert f"rt:pgmmerge:{beta}:{alpha}" in [
        b.callback_data for row in kb.inline_keyboard for b in row
    ]


async def test_confirming_the_merge_joins_them(fresh_db, user_id):
    db = fresh_db
    alpha = await _program(db, user_id, name="Альфа", days=("A1",))
    beta = await _program(db, user_id, name="Бета", days=("B1",))

    await routines.rt_program_merge(
        _make_callback(user_id, f"rt:pgmmerge:{beta}:{alpha}"), await _state(user_id)
    )

    assert {p["name"]: p["day_count"] for p in await db.list_programs(user_id)} == {"Альфа": 2}


async def test_an_over_long_name_is_refused_rather_than_breaking_the_list(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)
    state = await _state(user_id)
    await routines.rt_program_rename(_make_callback(user_id, f"rt:pgmrename:{program_id}"), state)

    message = _make_message(user_id, "х" * (config.MAX_PROGRAM_NAME_LENGTH + 1))
    await routines.rt_program_rename_entered(message, state)

    assert "Слишком длинное" in message.reply.await_args.args[0]
    assert (await db.get_program(program_id))["name"] == "PPL"


# ---------- состав дня ----------


async def test_the_scheme_can_be_changed_without_removing_the_exercise(fresh_db, user_id):
    """Раньше «3×10» → «4×8» стоило до девяти тапов и теряло позицию упражнения."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    first = await db.create_exercise(user_id, "Жим лёжа", group_id)
    second = await db.create_exercise(user_id, "Разводка", group_id)
    routine_id = await db.create_routine(user_id, "Толкай")
    await db.add_routine_exercise(routine_id, first, 0, "3×10")
    await db.add_routine_exercise(routine_id, second, 1, "3×12")
    entry = (await db.list_routine_exercises(routine_id))[0]
    state = await _state(user_id)

    await routines.rt_edit_exercise_target(
        _make_callback(user_id, f"rt:extarget:{routine_id}:{entry['id']}"), state
    )
    assert await state.get_state() == RoutineFlow.editing_exercise_target
    await routines.rt_exercise_target_entered(_make_message(user_id, "4×8"), state)

    after = await db.list_routine_exercises(routine_id)
    assert [(ex["display_name"], ex["target"]) for ex in after] == [
        ("Жим лёжа", "4×8"), ("Разводка", "3×12"),
    ]


async def test_the_scheme_can_be_cleared(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    routine_id = await db.create_routine(user_id, "Толкай")
    await db.add_routine_exercise(routine_id, ex_id, 0, "3×10")
    entry = (await db.list_routine_exercises(routine_id))[0]
    state = await _state(user_id)
    await routines.rt_edit_exercise_target(
        _make_callback(user_id, f"rt:extarget:{routine_id}:{entry['id']}"), state
    )

    await routines.rt_clear_exercise_target(_make_callback(user_id, "rt:extclear"), state)

    assert (await db.list_routine_exercises(routine_id))[0]["target"] is None


async def test_the_same_exercise_cannot_be_added_twice(fresh_db, user_id):
    """Дубль давал два одинаковых пункта в плане тренировки, а ручной выбор
    снимал с плана оба сразу."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    routine_id = await db.create_routine(user_id, "Толкай")
    await db.add_routine_exercise(routine_id, ex_id, 0, "3×10")
    state = await _state(user_id)
    await state.update_data(rtadd_routine_id=routine_id)

    callback = _make_callback(user_id)
    await routines._rtadd_finish(callback, state, ex_id, "4×8")

    assert len(await db.list_routine_exercises(routine_id)) == 1
    assert "уже здесь" in callback.answer.await_args.args[0]


# ---------- из тренировки ----------


async def test_a_snapshot_offers_to_become_a_multi_day_program(fresh_db, user_id):
    db = fresh_db
    workout_id = await db.create_workout(user_id)
    state = await _state(user_id)
    await state.update_data(routine_source_workout_id=workout_id)
    await state.set_state(RoutineFlow.naming)

    message = _make_message(user_id, "День A")
    await routines.rt_name_entered(message, state)

    routine_id = (await db.list_standalone_routines(user_id))[0]["id"]
    last_kb = message.answer.await_args.kwargs["reply_markup"]
    assert f"rt:tomulti:{routine_id}" in [
        b.callback_data for row in last_kb.inline_keyboard for b in row
    ]


async def test_a_standalone_snapshot_becomes_the_first_day_of_a_program(fresh_db, user_id):
    db = fresh_db
    routine_id = await db.create_routine(user_id, "День A")
    state = await _state(user_id)
    await routines.rt_to_multiday(_make_callback(user_id, f"rt:tomulti:{routine_id}"), state)

    await routines.rt_multiday_named(_make_message(user_id, "Мой А/Б"), state)

    programs = await db.list_programs(user_id)
    assert [(p["name"], p["day_count"]) for p in programs] == [("Мой А/Б", 1)]
    assert await db.list_standalone_routines(user_id) == []


async def test_a_further_day_can_come_from_another_workout(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("День A",))
    workout_id = await db.create_workout(user_id)
    state = await _state(user_id)
    await state.update_data(
        day_program_id=program_id, day_from_workout=True,
        routine_source_workout_id=workout_id,
    )
    await state.set_state(RoutineFlow.naming)

    await routines.rt_name_entered(_make_message(user_id, "День B"), state)

    assert [d["name"] for d in await db.list_program_days_by_id(program_id)] == ["День A", "День B"]


async def test_a_program_can_be_duplicated_whole(fresh_db, user_id):
    """Копия целиком — раньше вариант программы можно было получить только
    собрав её заново."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    program_id = await _program(db, user_id, days=("Толкай", "Тяни"))
    day = (await db.list_program_days_by_id(program_id))[0]
    await db.add_routine_exercise(day["id"], ex_id, 0, "4×8")
    entry = (await db.list_routine_exercises(day["id"]))[0]
    await db.set_routine_exercise_progression(entry["id"], '{"rule": "linear_load", "step": 2.5}')

    await routines.rt_program_copy(
        _make_callback(user_id, f"rt:pgmcopy:{program_id}"), await _state(user_id)
    )

    programs = {p["name"]: p["day_count"] for p in await db.list_programs(user_id)}
    assert programs == {"PPL": 2, "PPL (2)": 2}
    copy_id = (await db.find_program_by_name(user_id, "PPL (2)"))["id"]
    copied_day = (await db.list_program_days_by_id(copy_id))[0]
    copied = await db.list_routine_exercises(copied_day["id"])
    assert [(e["display_name"], e["target"]) for e in copied] == [("Жим лёжа", "4×8")]
    assert copied[0]["progression"] == '{"rule": "linear_load", "step": 2.5}'
    # Оригинал не тронут.
    assert len(await db.list_routine_exercises(day["id"])) == 1


async def test_duplicating_respects_the_day_budget(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай", "Тяни"))
    for i in range(config.MAX_ROUTINES_PER_USER - 2):
        await db.create_routine(user_id, f"Лишний {i}")

    callback = _make_callback(user_id, f"rt:pgmcopy:{program_id}")
    await routines.rt_program_copy(callback, await _state(user_id))

    assert "не влезет" in callback.answer.await_args.args[0]
    assert len(await db.list_programs(user_id)) == 1


# ---------- правки за одной кнопкой ----------


async def test_the_program_screen_is_about_training_not_editing(fresh_db, user_id):
    """Шесть кнопок редактирования стояли ровно на пути «пойти потренироваться»:
    экран трёхдневного сплита был из десяти кнопок, из которых по делу — три дня.
    Правки между тренировками не нужны примерно никогда, а экран открывают каждый
    раз."""
    db = fresh_db
    program_id = await _program(db, user_id)

    callback = _make_callback(user_id, f"rt:prg:{program_id}")
    await routines.rt_program(callback, await _state(user_id))

    callbacks = [cb for _text, cb in _buttons(callback)]
    assert f"rt:pgmedit:{program_id}" in callbacks
    for gone in ("rt:dayadd:", "rt:dayorder:", "rt:pgmcopy:", "rt:pgmrename:", "share:prg:", "rt:pgmdelask:"):
        assert not any(cb.startswith(gone) for cb in callbacks), gone
    # Осталось: три дня, «Изменить программу», «Назад».
    assert len(callbacks) == 5


async def test_every_edit_action_survived_the_move(fresh_db, user_id):
    """Кнопки не удалены, а переехали — если какая-то потерялась, действие
    становится недостижимым, потому что других входов у него нет."""
    db = fresh_db
    program_id = await _program(db, user_id)

    callback = _make_callback(user_id, f"rt:pgmedit:{program_id}")
    await routines.rt_program_edit(callback, await _state(user_id))

    callbacks = [cb for _text, cb in _buttons(callback)]
    assert callbacks == [
        f"rt:dayadd:{program_id}",
        f"rt:dayorder:{program_id}",
        f"rt:pgmcopy:{program_id}",
        f"rt:pgmrename:{program_id}",
        f"share:prg:{program_id}",
        f"rt:pgmdelask:{program_id}",
        f"rt:prg:{program_id}",
    ]


async def test_back_from_the_edit_screen_returns_to_the_program(fresh_db, user_id):
    """А не к списку программ: человек пришёл сюда с экрана программы и туда же
    ждёт вернуться."""
    db = fresh_db
    program_id = await _program(db, user_id)

    callback = _make_callback(user_id, f"rt:pgmedit:{program_id}")
    await routines.rt_program_edit(callback, await _state(user_id))

    back = [cb for text, cb in _buttons(callback) if text.startswith("⬅️")]
    assert back == [f"rt:prg:{program_id}"]


async def test_reordering_is_hidden_on_a_single_day_program(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id, days=("Всё тело",))

    callback = _make_callback(user_id, f"rt:pgmedit:{program_id}")
    await routines.rt_program_edit(callback, await _state(user_id))

    assert f"rt:dayorder:{program_id}" not in [cb for _text, cb in _buttons(callback)]


async def test_the_edit_screen_shows_what_is_being_edited(fresh_db, user_id):
    """Раньше экран правок сводился к имени и числу дней, и человек выбирал,
    какой день переименовать или куда добавить упражнение, не видя ни одного из
    них. Имя тоже обязано остаться: «Удалить» на безымянном экране страшно
    нажимать."""
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай", "Тяни"))

    callback = _make_callback(user_id, f"rt:pgmedit:{program_id}")
    await routines.rt_program_edit(callback, await _state(user_id))

    text = _last_text(callback)
    assert "PPL" in text
    assert "2 дня" in text
    assert "Толкай" in text and "Тяни" in text
    # Давность — про то, куда идти тренироваться, а не про то, что менять.
    assert "ещё не делал" not in text


async def test_the_edit_screen_belongs_to_its_owner(fresh_db, user_id):
    db = fresh_db
    program_id = await _program(db, user_id)

    callback = _make_callback(user_id + 1, f"rt:pgmedit:{program_id}")
    await routines.rt_program_edit(callback, await _state(user_id + 1))

    assert _buttons(callback) == []


# ---------- «незакрытая тренировка» перед стартом дня ----------


async def test_unfinished_workout_prompt_names_the_program_not_the_day(fresh_db, user_id):
    """routine['name'] — имя дня («Тяни»), а не программы («PPL»). Раньше
    фраза «начать по программе «Тяни»?» называла программой день — здесь
    должно быть имя программы, день — уточнением в скобках."""
    db = fresh_db
    program_id = await _program(db, user_id, days=("Толкай", "Тяни"))
    days = await db.list_program_days_by_id(program_id)
    tyani = next(d for d in days if d["name"] == "Тяни")
    # Незакрытая тренировка с хотя бы одним подходом — иначе бот тихо
    # выбрасывает старую и стартует новую без вопросов.
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    active_id = await db.create_workout(user_id, started_at="2026-08-01T10:00:00")
    block_id = await db.create_block(active_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5)

    callback = _make_callback(user_id, f"rt:start:{tyani['id']}")
    await routines.rt_start(callback, await _state(user_id))

    text = _last_text(callback)
    assert "программе «PPL»" in text
    assert "день «Тяни»" in text


async def test_unfinished_workout_prompt_has_no_program_name_for_standalone_day(fresh_db, user_id):
    """Тренировка без программы (routine['program_name'] is None) не должна
    получить пустое «программе « »» — используем имя самой тренировки."""
    db = fresh_db
    routine_id = await db.create_routine(user_id, "Фулбади")
    group_id = await db.create_muscle_group(user_id, "Ноги")
    ex_id = await db.create_exercise(user_id, "Присед", group_id)
    active_id = await db.create_workout(user_id, started_at="2026-08-01T10:00:00")
    block_id = await db.create_block(active_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5)

    callback = _make_callback(user_id, f"rt:start:{routine_id}")
    await routines.rt_start(callback, await _state(user_id))

    text = _last_text(callback)
    assert "программе «Фулбади»" in text
