"""Шаринг программ и упражнений: визитка → превью у получателя → явное «добавить».

Ничего не импортируется без согласия, шарится снапшот (владелец может удалить
оригинал — визитка живёт), имена при импорте резолвятся в три шага: своё →
шаблон каталога → создать под «Другое».
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import sharing

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=700))
    message.edit_reply_markup = AsyncMock()
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="TrainLogBot"))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.from_user = SimpleNamespace(id=user_id, username="recipient")
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=701))
    return msg


async def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def _routine_with_exercises(db, user_id: int):
    gid = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Жим лёжа", gid)
    custom = await db.create_exercise(user_id, "Моя авторская тяга", gid)
    routine_id = await db.create_routine(user_id, "Верх А")
    await db.add_routine_exercise(routine_id, bench, 0, "4×6–8")
    await db.add_routine_exercise(routine_id, custom, 1, None)
    return routine_id


async def _last_share_token(db) -> str:
    cur = await db.conn().execute("SELECT token FROM shared_items ORDER BY created_at DESC LIMIT 1")
    return (await cur.fetchone())["token"]


# ---------- визитка ----------


async def test_share_routine_snapshots_names_and_targets(fresh_db, user_id):
    db = fresh_db
    routine_id = await _routine_with_exercises(db, user_id)
    callback = _make_callback(user_id, f"share:rt:{routine_id}")

    await sharing.share_routine(callback, await _state(user_id))

    row = await db.get_shared_item(await _last_share_token(db))
    payload = json.loads(row["payload"])
    assert payload["name"] == "Верх А"
    assert payload["exercises"] == [
        {"name": "Жим лёжа", "target": "4×6–8"},
        {"name": "Моя авторская тяга", "target": None},
    ]
    # Визитка — с URL-кнопкой (переживает пересылку, в отличие от callback-кнопок).
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    url = kb.inline_keyboard[0][0].url
    assert url.startswith("https://t.me/TrainLogBot?start=sh_")


async def _program_with_two_days(db, user_id: int) -> int:
    gid = await db.create_muscle_group(user_id, "Ноги")
    squat = await db.create_exercise(user_id, "Присед", gid)
    bench = await db.create_exercise(user_id, "Жим лёжа", gid)
    day1 = await db.create_routine(user_id, "Ноги", program_name="Сплит")
    await db.add_routine_exercise(day1, squat, 0, "3×5")
    day2 = await db.create_routine(user_id, "Верх", program_name="Сплит")
    await db.add_routine_exercise(day2, bench, 0, "4×6–8")
    return day1  # anchor — любой день программы


async def test_share_program_snapshots_every_day(fresh_db, user_id):
    """«Поделиться программой» — вся многодневка одной визиткой, не день за раз."""
    db = fresh_db
    anchor_id = await _program_with_two_days(db, user_id)

    await sharing.share_program(_make_callback(user_id, f"share:pgm:{anchor_id}"), await _state(user_id))

    row = await db.get_shared_item(await _last_share_token(db))
    assert row["kind"] == "program"
    payload = json.loads(row["payload"])
    assert payload["name"] == "Сплит"
    assert [d["name"] for d in payload["days"]] == ["Ноги", "Верх"]
    assert payload["days"][0]["exercises"] == [{"name": "Присед", "target": "3×5"}]


async def test_accepting_a_shared_program_creates_one_routine_per_day(fresh_db, user_id):
    db = fresh_db
    anchor_id = await _program_with_two_days(db, user_id)
    await sharing.share_program(_make_callback(user_id, f"share:pgm:{anchor_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=555, username="r3"))["telegram_id"]
    await db.create_muscle_group(recipient, "Другое")
    callback = _make_callback(recipient, f"share:add:{token}")

    await sharing.share_add(callback, await _state(recipient))

    programs = await db.list_programs(recipient)
    assert [(p["program_name"], p["day_count"]) for p in programs] == [("Сплит", 2)]
    callback.message.edit_reply_markup.assert_awaited()


async def test_share_survives_deleting_the_original(fresh_db, user_id):
    """Шарится снапшот: удаление рутины после создания визитки не ломает ссылку."""
    db = fresh_db
    routine_id = await _routine_with_exercises(db, user_id)
    await sharing.share_routine(_make_callback(user_id, f"share:rt:{routine_id}"), await _state(user_id))
    token = await _last_share_token(db)

    await db.delete_routine(routine_id)

    row = await db.get_shared_item(token)
    assert row is not None
    assert json.loads(row["payload"])["name"] == "Верх А"


async def test_cannot_share_someone_elses_routine(fresh_db, user_id):
    db = fresh_db
    routine_id = await _routine_with_exercises(db, user_id)
    stranger = (await db.get_or_create_user(telegram_id=222, username="x"))["telegram_id"]
    callback = _make_callback(stranger, f"share:rt:{routine_id}")

    await sharing.share_routine(callback, await _state(stranger))

    callback.message.answer.assert_not_awaited()
    assert "не найдена" in callback.answer.await_args.args[0]


# ---------- превью у получателя ----------


async def test_recipient_sees_preview_and_is_asked_not_forced(fresh_db, user_id):
    db = fresh_db
    routine_id = await _routine_with_exercises(db, user_id)
    await sharing.share_routine(_make_callback(user_id, f"share:rt:{routine_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=333, username="r"))["telegram_id"]
    msg = _make_message(recipient)
    await sharing.open_shared(
        msg, CommandObject(command="start", args=f"sh_{token}"), await _state(recipient)
    )

    text = msg.answer.await_args.args[0]
    assert "Верх А" in text and "Жим лёжа" in text
    kb = msg.answer.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"share:add:{token}" in callbacks  # добавить — только явным тапом
    assert await db.list_routines(recipient) == []  # ничего не импортировано само


async def test_dead_token_does_not_break_start(fresh_db, user_id):
    msg = _make_message(user_id)
    await sharing.open_shared(
        msg, CommandObject(command="start", args="sh_nope"), await _state(user_id)
    )
    assert "устарела" in msg.answer.await_args.args[0]


async def test_owner_opening_own_card_is_not_offered_the_button(fresh_db, user_id):
    db = fresh_db
    routine_id = await _routine_with_exercises(db, user_id)
    await sharing.share_routine(_make_callback(user_id, f"share:rt:{routine_id}"), await _state(user_id))
    token = await _last_share_token(db)

    msg = _make_message(user_id)
    await sharing.open_shared(
        msg, CommandObject(command="start", args=f"sh_{token}"), await _state(user_id)
    )

    assert "твоя собственная" in msg.answer.await_args.args[0]
    assert msg.answer.await_args.kwargs.get("reply_markup") is None


# ---------- импорт программы ----------


async def test_accepting_imports_with_three_step_name_resolution(fresh_db, user_id):
    """Своё имя → переиспользуется; имя из каталога → форкается шаблон;
    кастомное чужое имя → создаётся под «Другое», а не выбрасывается."""
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Грудь")
    own_bench = await db.create_exercise(user_id, "Жим лёжа", gid)
    routine_id = await db.create_routine(user_id, "Микс")
    await db.add_routine_exercise(routine_id, own_bench, 0, "4×6–8")
    ex2 = await db.create_exercise(user_id, "Подтягивания", gid)
    await db.add_routine_exercise(routine_id, ex2, 1, None)
    ex3 = await db.create_exercise(user_id, "Секретная связка Саши", gid)
    await db.add_routine_exercise(routine_id, ex3, 2, "3×12")
    await sharing.share_routine(_make_callback(user_id, f"share:rt:{routine_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=444, username="r2"))["telegram_id"]
    rgid = await db.create_muscle_group(recipient, "Грудь")
    r_bench = await db.create_exercise(recipient, "жим лёжа", rgid)  # регистр другой — должно матчиться

    callback = _make_callback(recipient, f"share:add:{token}")
    await sharing.share_add(callback, await _state(recipient))

    routines = await db.list_routines(recipient)
    assert [r["name"] for r in routines] == ["Микс"]
    imported = await db.list_routine_exercises(routines[0]["id"])
    names = [e["display_name"] for e in imported]
    assert imported[0]["exercise_id"] == r_bench       # своё переиспользовано
    assert imported[0]["target"] == "4×6–8"            # target доехал
    assert "Секретная связка Саши" in " ".join(names)  # кастомное создано, не потеряно
    callback.message.edit_reply_markup.assert_awaited()  # кнопка снята — дубль не накликать


async def test_unknown_exercise_lands_in_the_fallback_group(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Спина")
    ex = await db.create_exercise(user_id, "Фирменное движение", gid)
    routine_id = await db.create_routine(user_id, "Соло")
    await db.add_routine_exercise(routine_id, ex, 0, None)
    await sharing.share_routine(_make_callback(user_id, f"share:rt:{routine_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=555, username="r3"))["telegram_id"]
    await sharing.share_add(_make_callback(recipient, f"share:add:{token}"), await _state(recipient))

    created = await db.find_exercise_by_name(recipient, "Фирменное движение")
    group = await db.get_muscle_group(created["primary_group_id"])
    assert group["name"] == "Другое"


# ---------- шаринг упражнения ----------


async def test_shared_exercise_carries_description_and_lands_in_matching_group(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Плечи")
    ex_id = await db.create_exercise(user_id, "Протяжка канатная", gid)
    await db.set_exercise_description(ex_id, "Тяни к подбородку, локти выше кистей.")
    await db.set_exercise_photo(ex_id, "PHOTO_FILE_ID_1")
    await sharing.share_exercise(_make_callback(user_id, f"share:ex:{ex_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=666, username="r4"))["telegram_id"]
    await db.create_muscle_group(recipient, "Плечи")
    await sharing.share_add(_make_callback(recipient, f"share:add:{token}"), await _state(recipient))

    created = await db.find_exercise_by_name(recipient, "Протяжка канатная")
    assert created["description"] == "Тяни к подбородку, локти выше кистей."
    assert created["custom_photo_file_id"] == "PHOTO_FILE_ID_1"
    group = await db.get_muscle_group(created["primary_group_id"])
    assert group["name"] == "Плечи"


async def test_accepting_an_exercise_you_already_have_does_not_duplicate(fresh_db, user_id):
    db = fresh_db
    gid = await db.create_muscle_group(user_id, "Плечи")
    ex_id = await db.create_exercise(user_id, "Протяжка", gid)
    await sharing.share_exercise(_make_callback(user_id, f"share:ex:{ex_id}"), await _state(user_id))
    token = await _last_share_token(db)

    recipient = (await db.get_or_create_user(telegram_id=777, username="r5"))["telegram_id"]
    rgid = await db.create_muscle_group(recipient, "Плечи")
    await db.create_exercise(recipient, "протяжка", rgid)
    before = await db.count_user_exercises(recipient)

    callback = _make_callback(recipient, f"share:add:{token}")
    await sharing.share_add(callback, await _state(recipient))

    assert await db.count_user_exercises(recipient) == before
    assert "уже есть" in callback.answer.await_args.args[0]
