"""«⧉ Сделать копию» — db.copy_exercise и обе двери в него: кнопка бота
(handlers/exercises.py) и POST /v1/exercises/{id}/copy.

Правила, которые здесь держатся:

- имя копии отличается и от источника, и от любого другого своего
  упражнения — сравнение то же, что у всех проверок уникальности
  (регистр, пробелы, ё=е, `name` и `display_name`);
- имя упражнения из АРХИВА тоже занято: уникальный индекс по display_name
  архив не исключает, а «Вернуть из архива» после такой копии дало бы два
  упражнения под одним именем — у этого случая свой текст с подсказкой;
- копия несёт настройки, описание, заметки и своё фото (файл копируется),
  но не историю, синонимы, программы, архив и пометки импорта/программы;
- копия — своё упражнение: `original_name` NULL, и идентичность каталога
  (дедуп, форк, поиск по идентичности) её с источником не путает, а
  картинки техники — те же, через `media_from`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import api_v1
import config
import exercise_media
import exercise_photos
import i18n
from fsm import ExerciseManage
from handlers import exercises

pytestmark = pytest.mark.asyncio

BENCH = "Жим штанги лёжа"


@pytest.fixture(autouse=True)
def photo_dir(tmp_path, monkeypatch):
    path = tmp_path / "exercise_photos"
    monkeypatch.setattr(config, "EXERCISE_PHOTO_DIR", str(path))
    return path


async def _group(db, user_id):
    return (await db.list_muscle_groups(user_id))[0]["id"]


async def _bench_fork(db, user_id) -> int:
    template = next(t for t in await db.list_all_exercise_templates() if t["name"] == BENCH)
    return await db.fork_exercise_from_template(user_id, template["id"])


# --- db.copy_exercise -------------------------------------------------------


async def test_copy_carries_settings_and_not_history(fresh_db, user_id):
    db = fresh_db
    gid = await _group(db, user_id)
    src = await db.create_exercise(
        user_id, "Подтягивания", gid, equipment="турник", unilateral=False, attachment="ремни",
        notes="локти вниз",
    )
    await db.set_exercise_description(src, "Тяни грудью к перекладине")
    await db.conn().execute(
        "UPDATE exercises SET bodyweight_load = 'full', bodyweight_factor = 0.9, "
        "import_batch_id = 'batch1', seeded_from_program = 1, last_used_at = '2026-01-01' WHERE id = ?",
        (src,),
    )
    await db.conn().commit()
    # История, синоним и место в программе у источника — у копии их быть не должно.
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, src, 0)
    await db.add_set(block_id, src, 0, 0, 20.0, 8)
    await db.conn().execute(
        "INSERT INTO exercise_aliases (user_id, folded_name, exercise_id, source, created_at) "
        "VALUES (?, 'подтяги', ?, 'manual', '2026-01-01')",
        (user_id, src),
    )
    await db.conn().commit()

    outcome, copy_id = await db.copy_exercise(user_id, src, "Подтягивания — узкий хват")
    assert outcome == db.COPY_OK
    copy = await db.get_exercise(copy_id)
    source = await db.get_exercise(src)

    for field in (
        "primary_group_id", "equipment", "unilateral", "attachment",
        "bodyweight_load", "bodyweight_factor", "description", "notes",
    ):
        assert copy[field] == source[field], field
    assert copy["name"] == "Подтягивания — узкий хват"
    assert copy["display_name"] == db.build_display_name(
        "Подтягивания — узкий хват", "турник", False, "ремни"
    )
    assert copy["user_id"] == user_id
    assert copy["is_archived"] == 0
    assert copy["is_template"] == 0
    assert copy["import_batch_id"] is None
    assert copy["seeded_from_program"] == 0
    assert copy["last_used_at"] is None
    assert copy["original_name"] is None
    assert copy["media_from"] == source["original_name"]

    assert await db.list_sets_for_exercise(copy_id) == []
    cur = await db.conn().execute("SELECT COUNT(*) FROM exercise_aliases WHERE exercise_id = ?", (copy_id,))
    assert (await cur.fetchone())[0] == 0
    cur = await db.conn().execute("SELECT COUNT(*) FROM block_exercises WHERE exercise_id = ?", (copy_id,))
    assert (await cur.fetchone())[0] == 0


async def test_copy_of_archived_source_is_live(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Шраги", await _group(db, user_id))
    await db.archive_exercise(src)
    outcome, copy_id = await db.copy_exercise(user_id, src, "Шраги с гантелями")
    assert outcome == db.COPY_OK
    assert (await db.get_exercise(copy_id))["is_archived"] == 0


async def test_copy_does_not_join_programs(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Жим ногами", await _group(db, user_id))
    program_id = await db.create_program(user_id, "Моя программа")
    routine_id = await db.create_routine(user_id, "День А", program_id=program_id)
    await db.add_routine_exercise(routine_id, src, 0, target="3x10")
    _, copy_id = await db.copy_exercise(user_id, src, "Жим ногами узко")
    cur = await db.conn().execute("SELECT COUNT(*) FROM routine_exercises WHERE exercise_id = ?", (copy_id,))
    assert (await cur.fetchone())[0] == 0


async def test_photo_file_is_duplicated_not_shared(fresh_db, user_id, photo_dir):
    db = fresh_db
    src = await db.create_exercise(user_id, "Тяга блока", await _group(db, user_id))
    name = exercise_photos.save(src, b"\x89PNGfake", "png")
    await db.set_exercise_photo(src, "TG_FILE", name)

    outcome, copy_id = await db.copy_exercise(user_id, src, "Тяга блока узко")
    assert outcome == db.COPY_OK
    copy = await db.get_exercise(copy_id)
    assert copy["custom_photo_file_id"] == "TG_FILE"
    assert copy["custom_photo_path"] and copy["custom_photo_path"] != name
    copy_path = exercise_photos.path_for_exercise(copy)
    assert copy_path is not None
    with open(copy_path, "rb") as fh:
        assert fh.read() == b"\x89PNGfake"

    # Удаление фото у источника не трогает файл копии — у каждого свой.
    await db.delete_exercise_photo(src)
    assert os.path.isfile(copy_path)


async def test_copy_without_photo_has_none(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Разгибания", await _group(db, user_id))
    _, copy_id = await db.copy_exercise(user_id, src, "Разгибания одной")
    copy = await db.get_exercise(copy_id)
    assert copy["custom_photo_path"] is None
    assert copy["custom_photo_file_id"] is None


@pytest.mark.parametrize("name", ["Жим гантелей", "жим ГАНТЕЛЕЙ", "  Жим гантелей  "])
async def test_same_name_as_source_is_refused(fresh_db, user_id, name):
    db = fresh_db
    src = await db.create_exercise(user_id, "Жим гантелей", await _group(db, user_id))
    outcome, copy_id = await db.copy_exercise(user_id, src, name)
    assert outcome == db.COPY_SAME_NAME
    assert copy_id is None


async def test_yo_and_e_count_as_the_same_name(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Жим лёжа", await _group(db, user_id))
    outcome, _ = await db.copy_exercise(user_id, src, "жим лежа")
    assert outcome == db.COPY_SAME_NAME


async def test_name_of_another_live_exercise_is_taken(fresh_db, user_id):
    db = fresh_db
    gid = await _group(db, user_id)
    src = await db.create_exercise(user_id, "Жим лёжа", gid)
    await db.create_exercise(user_id, "Жим лёжа узкий", gid)
    outcome, copy_id = await db.copy_exercise(user_id, src, "ЖИМ ЛЕЖА УЗКИЙ")
    assert outcome == db.COPY_NAME_TAKEN
    assert copy_id is None


async def test_display_name_of_another_exercise_is_taken(fresh_db, user_id):
    db = fresh_db
    gid = await _group(db, user_id)
    src = await db.create_exercise(user_id, "Тяга", gid)
    other = await db.create_exercise(user_id, "Тяга", gid, equipment="гантели")
    other_display = (await db.get_exercise(other))["display_name"]
    outcome, _ = await db.copy_exercise(user_id, src, other_display)
    assert outcome == db.COPY_NAME_TAKEN


async def test_name_of_an_archived_exercise_is_refused(fresh_db, user_id):
    """Решение: архив имя не освобождает (см. db._copy_name_clash)."""
    db = fresh_db
    gid = await _group(db, user_id)
    src = await db.create_exercise(user_id, "Выпады", gid)
    old = await db.create_exercise(user_id, "Выпады назад", gid)
    await db.archive_exercise(old)
    outcome, _ = await db.copy_exercise(user_id, src, "выпады назад")
    assert outcome == db.COPY_NAME_ARCHIVED


async def test_other_users_exercise_name_is_free(fresh_db, user_id):
    db = fresh_db
    other_user = user_id + 1
    await db.get_or_create_user(telegram_id=other_user, username="x")
    await db.create_exercise(other_user, "Жим узко", await _group(db, other_user))
    src = await db.create_exercise(user_id, "Жим", await _group(db, user_id))
    outcome, _ = await db.copy_exercise(user_id, src, "Жим узко")
    assert outcome == db.COPY_OK


async def test_foreign_or_missing_source_is_not_found(fresh_db, user_id):
    db = fresh_db
    other_user = user_id + 1
    await db.get_or_create_user(telegram_id=other_user, username="x")
    foreign = await db.create_exercise(other_user, "Чужое", await _group(db, other_user))
    assert (await db.copy_exercise(user_id, foreign, "Моё"))[0] == db.COPY_NOT_FOUND
    assert (await db.copy_exercise(user_id, 999999, "Моё"))[0] == db.COPY_NOT_FOUND
    template = (await db.list_all_exercise_templates())[0]
    assert (await db.copy_exercise(user_id, template["id"], "Моё"))[0] == db.COPY_NOT_FOUND


async def test_empty_name_is_refused(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Жим", await _group(db, user_id))
    assert (await db.copy_exercise(user_id, src, "   "))[0] == db.COPY_EMPTY_NAME


# --- идентичность против показа ----------------------------------------------


async def test_copy_of_catalog_exercise_shares_media_but_not_identity(fresh_db, user_id, tmp_path, monkeypatch):
    """Картинки техники у копии — те же, что у источника (клип и пара кадров),
    а дедуп/поиск по идентичности каталога копию с источником не путают."""
    db = fresh_db
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[BENCH]
    for name in (exercise_media.frame_name(slug, 1), exercise_media.frame_name(slug, 2), f"{slug}_demo.mp4"):
        (media_dir / name).write_bytes(b"x")
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(media_dir))

    src = await _bench_fork(db, user_id)
    _, copy_id = await db.copy_exercise(user_id, src, "Жим лёжа — узкий хват")
    source, copy = await db.get_exercise(src), await db.get_exercise(copy_id)

    # Те же демонстрации.
    assert exercise_media.get_images_for(source)
    assert exercise_media.get_images_for(copy) == exercise_media.get_images_for(source)
    assert exercise_media.get_animation_for(copy) == exercise_media.get_animation_for(source)
    assert exercise_media.get_animation_for(copy) is not None
    assert exercise_media.thumb_url_for(copy) == exercise_media.thumb_url_for(source)

    # Но не та же идентичность.
    assert copy["original_name"] is None
    assert copy["media_from"] == BENCH
    assert exercise_media.catalog_key(copy) != exercise_media.catalog_key(source)
    found = await db.find_exercise_by_original_name(user_id, BENCH)
    assert found["id"] == src
    identities = [r["id"] for r in await db.list_active_exercises_with_identity(user_id)]
    assert copy_id not in identities
    # Повторный форк того же шаблона узнаёт источник, а не копию.
    assert await _bench_fork(db, user_id) == src
    # Если источник ушёл в архив, форк достаёт ЕГО, а не цепляется за копию.
    await db.archive_exercise(src)
    assert await _bench_fork(db, user_id) == src


async def test_template_search_does_not_treat_copy_as_the_template(fresh_db, user_id):
    """Копия не прячет шаблон каталога из поиска: шаблон «уже есть» только
    у того, чья идентичность — этот шаблон."""
    db = fresh_db
    src = await _bench_fork(db, user_id)
    _, copy_id = await db.copy_exercise(user_id, src, "Мой жим — узкий хват")
    # Источник из базы — значит у атлета больше нет упражнения этой идентичности.
    await db.conn().execute("DELETE FROM exercises WHERE id = ?", (src,))
    await db.conn().commit()
    found = await db.search_exercise_templates(user_id, BENCH)
    assert any(t["name"] == BENCH for t in found)


async def test_copy_of_a_copy_keeps_media_from(fresh_db, user_id):
    db = fresh_db
    src = await _bench_fork(db, user_id)
    _, first = await db.copy_exercise(user_id, src, "Жим узко")
    _, second = await db.copy_exercise(user_id, first, "Жим узко с паузой")
    copy2 = await db.get_exercise(second)
    assert copy2["original_name"] is None
    assert copy2["media_from"] == BENCH
    assert exercise_media.get_images_for(copy2) == exercise_media.get_images_for(await db.get_exercise(src))


# --- REST ---------------------------------------------------------------------

USER = 111


async def _linked(fresh_db, lang: str = "ru") -> httpx.AsyncClient:
    await fresh_db.get_or_create_user(telegram_id=USER, username="tester")
    await fresh_db.set_user_lang(USER, lang)
    code = await fresh_db.issue_oauth_link_code(USER, ttl_seconds=600, digits=8)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


async def test_api_copy_returns_201_with_exercise_shape(fresh_db):
    client = await _linked(fresh_db)
    gid = await _group(fresh_db, USER)
    src = await fresh_db.create_exercise(USER, "Жим лёжа", gid)
    plain = (await client.get("/exercises")).json()[0]

    resp = await client.post(f"/exercises/{src}/copy", json={"name": "Жим лёжа — узкий хват"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == "Жим лёжа — узкий хват"
    assert body["primary_group_id"] == gid
    assert body["original_name"] is None
    assert set(body) == set(plain) - {"set_count"}
    listed = {e["id"] for e in (await client.get("/exercises")).json()}
    assert body["id"] in listed


async def test_api_copy_errors(fresh_db):
    client = await _linked(fresh_db)
    gid = await _group(fresh_db, USER)
    src = await fresh_db.create_exercise(USER, "Жим лёжа", gid)
    await fresh_db.create_exercise(USER, "Жим узкий", gid)

    resp = await client.post(f"/exercises/{src}/copy", json={"name": "  "})
    assert resp.status_code == 400

    resp = await client.post(f"/exercises/{src}/copy", json={"name": "жим лёжа"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "exercise_name_taken"
    assert resp.json()["message"] == i18n.t_in("ru", "exercises.copy.same_name")

    resp = await client.post(f"/exercises/{src}/copy", json={"name": "Жим узкий"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "exercise_name_taken"
    assert resp.json()["message"] == (
        "Такое упражнение уже есть. Назови копию по-другому, например «Жим лёжа — узкий хват»."
    )

    resp = await client.post("/exercises/999999/copy", json={"name": "Новое"})
    assert resp.status_code == 404

    other = USER + 1
    await fresh_db.get_or_create_user(telegram_id=other, username="other")
    foreign = await fresh_db.create_exercise(other, "Чужое", await _group(fresh_db, other))
    resp = await client.post(f"/exercises/{foreign}/copy", json={"name": "Моё"})
    assert resp.status_code == 404


async def test_api_copy_error_is_in_english_for_english_athlete(fresh_db):
    client = await _linked(fresh_db, lang="en")
    src = await fresh_db.create_exercise(USER, "Bench Press", await _group(fresh_db, USER))
    resp = await client.post(f"/exercises/{src}/copy", json={"name": "bench press"})
    assert resp.status_code == 409
    assert resp.json()["message"] == i18n.t_in("en", "exercises.copy.same_name")


# --- бот ----------------------------------------------------------------------


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
    message.delete = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _message(user_id: int, text: str):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id, language_code=None)
    message.chat = SimpleNamespace(id=user_id)
    message.text = text
    message.reply = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=600))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=601, photo=None))

    async def _media_group(*args, **kwargs):
        return [SimpleNamespace(message_id=700), SimpleNamespace(message_id=701)]

    message.answer_media_group = AsyncMock(side_effect=_media_group)
    message.answer_animation = AsyncMock(return_value=SimpleNamespace(message_id=702, animation=None))
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    message.bot = bot
    return message


async def _state(user_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(ExerciseManage.picking_exercise)
    return state


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


async def test_edit_menu_has_copy_next_to_merge():
    ex = {"id": 7, "name": "x", "display_name": "x", "description": None, "custom_photo_file_id": None}
    rows = exercises._exercise_edit_menu_keyboard(ex).inline_keyboard
    merge_row = next(row for row in rows if any(b.callback_data == "exm:mergestart:7" for b in row))
    assert [b.callback_data for b in merge_row] == ["exm:mergestart:7", "exm:copystart:7"]
    assert merge_row[1].text == "⧉ Сделать копию"


async def test_copy_button_asks_for_name_with_cancel_to_card(fresh_db, user_id):
    src = await fresh_db.create_exercise(user_id, "Жим лёжа", await _group(fresh_db, user_id))
    state = await _state(user_id)
    cb = _callback(user_id, f"exm:copystart:{src}")
    await exercises.exm_copy_start(cb, state)
    assert await state.get_state() == ExerciseManage.copying_name.state
    text = cb.message.answer.await_args.args[0]
    assert text == (
        "Как назовём копию? Имя должно отличаться от «Жим лёжа» — например, «Жим лёжа — обратный хват»."
    )
    kb = cb.message.answer.await_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        ("✖️ Отмена", f"exm:ex:{src}")
    ]


async def test_copy_button_refuses_foreign_exercise(fresh_db, user_id):
    other = user_id + 1
    await fresh_db.get_or_create_user(telegram_id=other, username="x")
    foreign = await fresh_db.create_exercise(other, "Чужое", await _group(fresh_db, other))
    state = await _state(user_id)
    cb = _callback(user_id, f"exm:copystart:{foreign}")
    await exercises.exm_copy_start(cb, state)
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await state.get_state() == ExerciseManage.picking_exercise.state


async def test_same_name_reply_then_success_shows_new_card(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Жим лёжа", await _group(db, user_id))
    state = await _state(user_id)
    await exercises.exm_copy_start(_callback(user_id, f"exm:copystart:{src}"), state)

    msg = _message(user_id, "ЖИМ ЛЁЖА")
    await exercises.exm_copy_name_entered(msg, state)
    assert msg.reply.await_args.args[0] == (
        "Это то же самое имя. Допиши, чем копия отличается — хват, наклон, снаряд."
    )
    assert await state.get_state() == ExerciseManage.copying_name.state

    msg = _message(user_id, "Жим лёжа — узкий хват")
    await exercises.exm_copy_name_entered(msg, state)
    sent = [c.args[0] for c in msg.answer.await_args_list]
    assert sent[0] == "Сделал копию «Жим лёжа — узкий хват». Подходы у неё свои, с нуля."
    copy = await db.find_exercise_by_name(user_id, "Жим лёжа — узкий хват")
    assert copy is not None
    data = await state.get_data()
    assert data["exm_exercise_id"] == copy["id"]
    assert await state.get_state() == ExerciseManage.picking_exercise.state
    # Последнее сообщение — карточка новой копии.
    card_kb = msg.answer.await_args_list[-1].kwargs["reply_markup"]
    assert f"exm:editmenu:{copy['id']}" in [b.callback_data for row in card_kb.inline_keyboard for b in row]


async def test_taken_name_reply_quotes_source_example(fresh_db, user_id):
    db = fresh_db
    gid = await _group(db, user_id)
    src = await db.create_exercise(user_id, "Жим лёжа", gid)
    await db.create_exercise(user_id, "Жим узкий", gid)
    state = await _state(user_id)
    await exercises.exm_copy_start(_callback(user_id, f"exm:copystart:{src}"), state)
    msg = _message(user_id, "жим узкий")
    await exercises.exm_copy_name_entered(msg, state)
    assert msg.reply.await_args.args[0] == (
        "Такое упражнение уже есть. Назови копию по-другому, например «Жим лёжа — узкий хват»."
    )
    assert _labels(msg.reply.await_args.kwargs["reply_markup"]) == ["✖️ Отмена"]


async def test_too_long_name_is_refused_with_limit(fresh_db, user_id):
    src = await fresh_db.create_exercise(user_id, "Жим", await _group(fresh_db, user_id))
    state = await _state(user_id)
    await exercises.exm_copy_start(_callback(user_id, f"exm:copystart:{src}"), state)
    msg = _message(user_id, "Ж" * (config.MAX_EXERCISE_NAME_LENGTH + 1))
    await exercises.exm_copy_name_entered(msg, state)
    assert str(config.MAX_EXERCISE_NAME_LENGTH) in msg.reply.await_args.args[0]
    assert len(await fresh_db.list_user_exercises(user_id)) == 1


async def test_english_copy_prompt_and_done(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    src = await db.create_exercise(user_id, "Bench Press", await _group(db, user_id))
    state = await _state(user_id)
    with i18n.use_lang("en"):
        cb = _callback(user_id, f"exm:copystart:{src}")
        await exercises.exm_copy_start(cb, state)
        prompt = cb.message.answer.await_args.args[0]
        msg = _message(user_id, "Bench Press — close grip")
        await exercises.exm_copy_name_entered(msg, state)
    assert prompt == (
        'What do we call the copy? The name has to differ from "Bench Press" — say, '
        '"Bench Press — reverse grip".'
    )
    assert msg.answer.await_args_list[0].args[0] == (
        'Made a copy: "Bench Press — close grip". It gets its own sets, starting from zero.'
    )


# --- имя показа источника в поле (приложение подставляет display_name) --------


async def test_display_name_with_appended_text_does_not_double_suffix(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(user_id, "Тяга", await _group(db, user_id), equipment="гантели")
    source = await db.get_exercise(src)
    assert source["display_name"] == db.build_display_name("Тяга", "гантели")

    outcome, copy_id = await db.copy_exercise(user_id, src, source["display_name"] + " — узкий хват")
    assert outcome == db.COPY_OK
    copy = await db.get_exercise(copy_id)
    assert copy["name"] == "Тяга — узкий хват"
    assert copy["display_name"] == db.build_display_name("Тяга — узкий хват", "гантели")
    assert copy["display_name"].count("гантели") == 1


async def test_display_name_sent_unchanged_is_the_same_name(fresh_db, user_id):
    db = fresh_db
    src = await db.create_exercise(
        user_id, "Тяга", await _group(db, user_id), equipment="штанга", unilateral=True, attachment="канат"
    )
    display = (await db.get_exercise(src))["display_name"]
    for sent in (display, display.upper(), f"  {display}  "):
        outcome, copy_id = await db.copy_exercise(user_id, src, sent)
        assert outcome == db.COPY_SAME_NAME, sent
        assert copy_id is None


async def test_api_display_name_suffix_is_not_doubled_and_unchanged_is_refused(fresh_db):
    client = await _linked(fresh_db)
    src = await fresh_db.create_exercise(USER, "Тяга", await _group(fresh_db, USER), equipment="гантели")
    display = (await fresh_db.get_exercise(src))["display_name"]

    resp = await client.post(f"/exercises/{src}/copy", json={"name": display})
    assert resp.status_code == 409
    assert resp.json()["error"] == "exercise_name_taken"
    assert resp.json()["message"] == i18n.t_in("ru", "exercises.copy.same_name")

    resp = await client.post(f"/exercises/{src}/copy", json={"name": display + " — узкий хват"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == fresh_db.build_display_name("Тяга — узкий хват", "гантели")
    assert body["display_name"].count("гантели") == 1
    assert (await fresh_db.get_exercise(body["id"]))["name"] == "Тяга — узкий хват"
