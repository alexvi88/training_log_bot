"""«⧉ Сделать копию» шаблона каталога — db.copy_template_exercise и обе двери
в него: превью шаблона в боте (handlers/exercises.py, `exm:tplcopy`) и
POST /v1/exercise-templates/{id}/copy.

Правила те же, что у копии своего упражнения (tests/test_exercise_copy.py),
и живут в одном месте (db._insert_copy):

- копия несёт группу, снаряд, одну руку, насадку, режим и долю веса тела и
  описание техники каталога на языке атлета;
- картинки техники — шаблона (`media_from` = его идентичность), а сама
  идентичность — нет: `original_name` NULL, и «Добавить», дедуп, поиск по
  идентичности, импорт и смена языка копию с шаблоном не склеивают;
- имя отличается и от шаблона (на языке атлета и от русской идентичности),
  и от любого своего упражнения, архив включительно.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import api_v1
import config
import exercise_descriptions
import exercise_media
import i18n
import keyboards
from fsm import ExerciseManage
from handlers import csv_import, exercises

pytestmark = pytest.mark.asyncio

BENCH = "Жим штанги лёжа"
PULLUP = "Подтягивания"


@pytest.fixture(autouse=True)
def photo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_PHOTO_DIR", str(tmp_path / "exercise_photos"))


async def _template(db, name: str = BENCH):
    return next(t for t in await db.list_all_exercise_templates() if t["name"] == name)


async def _group(db, user_id):
    return (await db.list_muscle_groups(user_id))[0]["id"]


# --- db.copy_template_exercise -------------------------------------------------


async def test_copy_carries_template_settings_and_description(fresh_db, user_id):
    db = fresh_db
    template = await _template(db, PULLUP)
    assert template["bodyweight_load"] == "full"
    outcome, copy_id = await db.copy_template_exercise(user_id, template["id"], "Подтягивания с паузой")
    assert outcome == db.COPY_OK
    copy = await db.get_exercise(copy_id)
    for field in (
        "primary_group_id", "equipment", "unilateral", "attachment",
        "bodyweight_load", "bodyweight_factor",
    ):
        assert copy[field] == template[field], field
    assert copy["user_id"] == user_id
    assert copy["is_template"] == 0
    assert copy["name"] == "Подтягивания с паузой"
    assert copy["original_name"] is None
    assert copy["media_from"] == PULLUP
    assert copy["notes"] is None
    assert copy["custom_photo_file_id"] is None
    assert copy["description"] == exercise_descriptions.get_description(PULLUP, "ru")
    assert copy["description"]
    # Шаблон не тронут.
    assert (await db.get_exercise(template["id"]))["user_id"] is None


async def test_description_is_in_the_athletes_language(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = await _template(db)
    _, copy_id = await db.copy_template_exercise(user_id, template["id"], "Paused bench")
    copy = await db.get_exercise(copy_id)
    assert copy["description"] == exercise_descriptions.get_description(BENCH, "en")
    assert copy["description"] != exercise_descriptions.get_description(BENCH, "ru")


async def test_copy_shows_template_pictures(fresh_db, user_id, tmp_path, monkeypatch):
    db = fresh_db
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[BENCH]
    for name in (exercise_media.frame_name(slug, 1), exercise_media.frame_name(slug, 2), f"{slug}_demo.mp4"):
        (media_dir / name).write_bytes(b"x")
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(media_dir))
    template = await _template(db)
    _, copy_id = await db.copy_template_exercise(user_id, template["id"], "Жим с паузой")
    copy = await db.get_exercise(copy_id)
    assert exercise_media.get_images_for(copy) == exercise_media.get_images_for(template)
    assert exercise_media.get_images_for(copy)
    assert exercise_media.get_animation_for(copy) == exercise_media.get_animation_for(template)
    assert exercise_media.thumb_url_for(copy) == exercise_media.thumb_url_for(template)


async def test_copy_is_not_the_catalog_exercise(fresh_db, user_id):
    """«Добавить» после копии заводит сам шаблон, а не находит копию; поиск
    каталога по-прежнему показывает шаблон; идентичность — только у форка."""
    db = fresh_db
    template = await _template(db)
    _, copy_id = await db.copy_template_exercise(user_id, template["id"], "Мой жим с паузой")
    copy = await db.get_exercise(copy_id)
    assert exercise_media.catalog_key(copy) != BENCH
    assert exercise_descriptions.catalog_description(copy, "ru") is None
    assert await db.find_exercise_by_original_name(user_id, BENCH) is None
    assert copy_id not in [r["id"] for r in await db.list_active_exercises_with_identity(user_id)]
    found = await db.search_exercise_templates(user_id, BENCH)
    assert any(t["name"] == BENCH for t in found)

    fork_id = await db.fork_exercise_from_template(user_id, template["id"])
    assert fork_id != copy_id
    assert (await db.get_exercise(fork_id))["original_name"] == BENCH
    assert (await db.get_exercise(copy_id))["original_name"] is None


async def test_import_does_not_resolve_catalog_name_to_the_copy(fresh_db, user_id):
    db = fresh_db
    template = await _template(db)
    _, copy_id = await db.copy_template_exercise(user_id, template["id"], "Мой жим с паузой")
    matches = await csv_import.match_exercise_names(user_id, [BENCH, "Bench Press (Barbell)"])
    for name, match in matches.items():
        assert match.exercise_id != copy_id, name


async def test_language_switch_does_not_rename_the_copy(fresh_db, user_id):
    db = fresh_db
    template = await _template(db)
    _, copy_id = await db.copy_template_exercise(user_id, template["id"], "Мой жим с паузой")
    await db.set_user_lang(user_id, "en")
    copy = await db.get_exercise(copy_id)
    assert copy["name"] == "Мой жим с паузой"
    assert copy["original_name"] is None


@pytest.mark.parametrize("name", [BENCH, "жим ШТАНГИ лёжа", "  Жим штанги лежа  "])
async def test_template_identity_name_is_refused(fresh_db, user_id, name):
    db = fresh_db
    template = await _template(db)
    assert (await db.copy_template_exercise(user_id, template["id"], name))[0] == db.COPY_SAME_NAME


async def test_template_name_in_athletes_language_is_refused(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = await _template(db)
    shown = i18n.t_in("en", f"exercise.{exercise_media.EXERCISE_IMAGE_SLUGS[BENCH]}.name")
    outcome, _ = await db.copy_template_exercise(user_id, template["id"], shown.upper())
    assert outcome == db.COPY_SAME_NAME
    # Русская идентичность тоже то же имя — и у англоязычного.
    assert (await db.copy_template_exercise(user_id, template["id"], BENCH))[0] == db.COPY_SAME_NAME


async def test_name_of_own_live_and_archived_exercise_is_refused(fresh_db, user_id):
    db = fresh_db
    gid = await _group(db, user_id)
    template = await _template(db)
    await db.create_exercise(user_id, "Жим узкий", gid)
    archived = await db.create_exercise(user_id, "Жим старый", gid)
    await db.archive_exercise(archived)
    assert (await db.copy_template_exercise(user_id, template["id"], "жим узкий"))[0] == db.COPY_NAME_TAKEN
    assert (await db.copy_template_exercise(user_id, template["id"], "Жим старый"))[0] == db.COPY_NAME_ARCHIVED
    assert (await db.copy_template_exercise(user_id, template["id"], "   "))[0] == db.COPY_EMPTY_NAME
    # Второй раз то же имя копии — занято первой копией.
    assert (await db.copy_template_exercise(user_id, template["id"], "Жим с паузой"))[0] == db.COPY_OK
    assert (await db.copy_template_exercise(user_id, template["id"], "Жим с паузой"))[0] == db.COPY_NAME_TAKEN


async def test_own_exercise_or_missing_id_is_not_a_template(fresh_db, user_id):
    db = fresh_db
    own = await db.create_exercise(user_id, "Своё", await _group(db, user_id))
    assert (await db.copy_template_exercise(user_id, own, "Своё 2"))[0] == db.COPY_NOT_FOUND
    assert (await db.copy_template_exercise(user_id, 999999, "Новое"))[0] == db.COPY_NOT_FOUND


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


async def test_api_template_copy_returns_201_with_exercise_shape(fresh_db):
    client = await _linked(fresh_db)
    template = await _template(fresh_db)
    added = (await client.post(f"/exercise-templates/{template['id']}/add")).json()

    resp = await client.post(f"/exercise-templates/{template['id']}/copy", json={"name": "Жим с паузой"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == set(added)
    assert body["display_name"] == "Жим с паузой"
    assert body["original_name"] is None
    assert body["primary_group_id"] == template["primary_group_id"]
    assert body["id"] != added["id"]
    assert body["id"] in {e["id"] for e in (await client.get("/exercises")).json()}


async def test_api_template_copy_errors_ru(fresh_db):
    client = await _linked(fresh_db)
    template = await _template(fresh_db)
    await fresh_db.create_exercise(USER, "Жим узкий", await _group(fresh_db, USER))
    url = f"/exercise-templates/{template['id']}/copy"

    assert (await client.post(url, json={"name": "  "})).status_code == 400

    resp = await client.post(url, json={"name": "жим штанги лёжа"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "exercise_name_taken"
    assert resp.json()["message"] == i18n.t_in("ru", "exercises.copy.same_name")

    resp = await client.post(url, json={"name": "Жим узкий"})
    assert resp.status_code == 409
    assert resp.json()["message"] == (
        "Такое упражнение уже есть. Назови копию по-другому, например «Жим штанги лёжа — узкий хват»."
    )

    assert (await client.post("/exercise-templates/999999/copy", json={"name": "X"})).status_code == 404
    own = (await client.get("/exercises")).json()[0]["id"]
    assert (await client.post(f"/exercise-templates/{own}/copy", json={"name": "X"})).status_code == 404


async def test_api_template_copy_errors_en(fresh_db):
    client = await _linked(fresh_db, lang="en")
    template = await _template(fresh_db)
    shown = i18n.t_in("en", f"exercise.{exercise_media.EXERCISE_IMAGE_SLUGS[BENCH]}.name")
    url = f"/exercise-templates/{template['id']}/copy"

    resp = await client.post(url, json={"name": shown.lower()})
    assert resp.status_code == 409
    assert resp.json()["message"] == i18n.t_in("en", "exercises.copy.same_name")

    await client.post(url, json={"name": "Paused bench"})
    resp = await client.post(url, json={"name": "paused bench"})
    assert resp.status_code == 409
    assert resp.json()["message"] == i18n.t_in("en", "exercises.copy.name_taken", source=shown)

    resp = await client.post(url, json={"name": "Bench with a pause"})
    assert resp.status_code == 201
    assert resp.json()["display_name"] == "Bench with a pause"


# --- бот ----------------------------------------------------------------------


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    message.text = None
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=501, photo=None))
    message.answer_animation = AsyncMock(return_value=SimpleNamespace(message_id=502, animation=None))

    async def _media_group(*args, **kwargs):
        return [SimpleNamespace(message_id=503), SimpleNamespace(message_id=504)]

    message.answer_media_group = AsyncMock(side_effect=_media_group)
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


async def _state(user_id: int, group_id: int) -> FSMContext:
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(ExerciseManage.creating_exercise_name)
    await state.update_data(exm_group_id=group_id)
    return state


def _buttons(kb) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


async def test_preview_keyboard_has_copy_under_add():
    assert _buttons(keyboards.template_preview_keyboard(5, with_copy=True)) == [
        ("➕ Добавить", "exm:tpladd:5"),
        ("⧉ Сделать копию", "exm:tplcopy:5"),
        ("⬅️ Назад", "exm:templates"),
    ]
    # Без флага (тренировка, программа) — как было.
    assert "pick:tplcopy:5" not in [c for _, c in _buttons(keyboards.template_preview_keyboard(5, prefix="pick"))]


async def test_template_preview_offers_copy(fresh_db, user_id):
    template = await _template(fresh_db)
    state = await _state(user_id, template["primary_group_id"])
    cb = _callback(user_id, f"exm:tpl:{template['id']}")
    await exercises.exm_preview_template(cb, state)
    sent = [c.kwargs.get("reply_markup") for m in (cb.message.answer, cb.message.answer_photo, cb.message.answer_animation)
            for c in m.await_args_list]
    callbacks = [c for kb in sent if kb is not None for _, c in _buttons(kb)]
    assert f"exm:tplcopy:{template['id']}" in callbacks


async def test_copy_flow_from_template_preview(fresh_db, user_id):
    db = fresh_db
    template = await _template(db)
    gid = template["primary_group_id"]
    state = await _state(user_id, gid)
    cb = _callback(user_id, f"exm:tplcopy:{template['id']}")
    await exercises.exm_template_copy_start(cb, state)
    assert await state.get_state() == ExerciseManage.copying_name.state
    assert cb.message.answer.await_args.args[0] == (
        "Как назовём копию? Имя должно отличаться от «Жим штанги лёжа» — например, "
        "«Жим штанги лёжа — обратный хват»."
    )
    assert _buttons(cb.message.answer.await_args.kwargs["reply_markup"]) == [
        ("✖️ Отмена", f"exm:tpl:{template['id']}")
    ]

    msg = _message(user_id, "жим штанги лёжа")
    await exercises.exm_copy_name_entered(msg, state)
    assert msg.reply.await_args.args[0] == i18n.t_in("ru", "exercises.copy.same_name")
    assert _buttons(msg.reply.await_args.kwargs["reply_markup"]) == [("✖️ Отмена", f"exm:tpl:{template['id']}")]

    msg = _message(user_id, "Жим с паузой")
    await exercises.exm_copy_name_entered(msg, state)
    assert msg.answer.await_args_list[0].args[0] == "Сделал копию «Жим с паузой». Подходы у неё свои, с нуля."
    copy = await db.find_exercise_by_name(user_id, "Жим с паузой")
    assert copy["original_name"] is None and copy["media_from"] == BENCH
    assert (await state.get_data())["exm_exercise_id"] == copy["id"]
    assert await state.get_state() == ExerciseManage.picking_exercise.state


async def test_cancel_returns_to_template_preview(fresh_db, user_id):
    template = await _template(fresh_db)
    state = await _state(user_id, template["primary_group_id"])
    await exercises.exm_template_copy_start(_callback(user_id, f"exm:tplcopy:{template['id']}"), state)
    await exercises.exm_preview_template(_callback(user_id, f"exm:tpl:{template['id']}"), state)
    assert await state.get_state() == ExerciseManage.creating_exercise_name.state
    assert (await state.get_data())["exm_copy_template_id"] is None
    assert await fresh_db.list_user_exercises(user_id) == []


async def test_copy_of_own_exercise_after_template_copy_uses_own_source(fresh_db, user_id):
    """Ключ шаблона из прошлого захода не перехватывает копию своего упражнения."""
    db = fresh_db
    template = await _template(db)
    state = await _state(user_id, template["primary_group_id"])
    await exercises.exm_template_copy_start(_callback(user_id, f"exm:tplcopy:{template['id']}"), state)
    own = await db.create_exercise(user_id, "Тяга", template["primary_group_id"])
    await state.set_state(ExerciseManage.picking_exercise)
    await exercises.exm_copy_start(_callback(user_id, f"exm:copystart:{own}"), state)
    await exercises.exm_copy_name_entered(_message(user_id, "Тяга узко"), state)
    copy = await db.find_exercise_by_name(user_id, "Тяга узко")
    assert copy is not None and copy["media_from"] == "Тяга"


async def test_english_template_copy_prompt(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    template = await _template(db)
    shown = i18n.t_in("en", f"exercise.{exercise_media.EXERCISE_IMAGE_SLUGS[BENCH]}.name")
    state = await _state(user_id, template["primary_group_id"])
    with i18n.use_lang("en"):
        cb = _callback(user_id, f"exm:tplcopy:{template['id']}")
        await exercises.exm_template_copy_start(cb, state)
        msg = _message(user_id, shown)
        await exercises.exm_copy_name_entered(msg, state)
    assert cb.message.answer.await_args.args[0] == i18n.t_in("en", "exercises.copy.prompt", source=shown)
    assert msg.reply.await_args.args[0] == i18n.t_in("en", "exercises.copy.same_name")
