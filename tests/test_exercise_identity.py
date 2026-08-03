"""Что упражнение теряет и не теряет, когда его правят.

Имя упражнения — изменяемое поле, которое пользователю прямо предлагают
поменять. До этих правок на нём висело всё: каталожная техника, демо-фото и
защита от дублей — и переименование сносило первые два и обходило третью.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import exercise_descriptions
import exercise_media
from handlers import exercise_resolve, exercises

# asyncio_mode=auto (pytest.ini) — async-тестам маркер не нужен, а часть проверок
# ниже синхронные.


async def _forked_with_assets(db, user_id):
    """Форк каталожного упражнения, у которого точно есть и текст, и фото."""
    template = next(
        t for t in await db.list_all_exercise_templates()
        if t["name"] in exercise_media.EXERCISE_IMAGE_SLUGS
        and exercise_descriptions.get_description(t["name"])
    )
    return await db.fork_exercise_from_template(user_id, template["id"]), template["name"]


# ---------- E1: техника и фото переживают переименование ----------


async def test_renaming_keeps_the_catalog_technique_and_photos(fresh_db, user_id):
    db = fresh_db
    ex_id, template_name = await _forked_with_assets(db, user_id)

    await db.update_exercise_name(ex_id, "Моё название")
    ex = await db.get_exercise(ex_id)

    assert ex["display_name"] == "Моё название"
    assert exercise_descriptions.effective_description(ex) == (
        exercise_descriptions.get_description(template_name)
    )
    assert len(exercise_media.get_images_for(ex)) == 2


async def test_a_personal_description_still_wins_over_the_catalog_one(fresh_db, user_id):
    db = fresh_db
    ex_id, _ = await _forked_with_assets(db, user_id)
    await db.set_exercise_description(ex_id, "мои заметки по технике")

    await db.update_exercise_name(ex_id, "Моё название")

    ex = await db.get_exercise(ex_id)
    assert exercise_descriptions.effective_description(ex) == "мои заметки по технике"


async def test_an_exercise_the_user_invented_has_no_catalog_assets(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Тяга саней по парковке", group_id)

    ex = await db.get_exercise(ex_id)

    assert exercise_descriptions.effective_description(ex) is None
    assert exercise_media.get_images_for(ex) == []


def test_catalog_key_falls_back_when_the_column_is_absent():
    """Строка могла приехать из запроса, который original_name не выбирал."""
    assert exercise_media.catalog_key({"name": "Жим лёжа"}) == "Жим лёжа"
    assert exercise_media.catalog_key(
        {"name": "Моё", "original_name": "Жим лёжа"}
    ) == "Жим лёжа"
    assert exercise_media.catalog_key({"name": "Жим лёжа", "original_name": None}) == "Жим лёжа"


# ---------- E2: переименование не создаёт дубль ----------


async def test_renaming_onto_an_existing_name_is_refused(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа", group_id)
    other = await db.create_exercise(user_id, "Разводка", group_id)

    assert await db.update_exercise_name(other, "Жим лёжа") is False
    assert (await db.get_exercise(other))["display_name"] == "Разводка"


async def test_the_refusal_folds_cyrillic_case(fresh_db, user_id):
    """UNIQUE-индекс стоит на SQL LOWER(), а он сворачивает только ASCII — на
    кириллице он не срабатывал никогда, и «ЖИМ ЛЁЖА» спокойно вставал рядом с
    «Жим лёжа»."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    await db.create_exercise(user_id, "Жим лёжа", group_id)
    other = await db.create_exercise(user_id, "Разводка", group_id)

    for clash in ("ЖИМ ЛЁЖА", "жим лёжа", "  Жим лёжа  "):
        assert await db.update_exercise_name(other, clash) is False, clash

    names = [e["display_name"] for e in await db.list_user_exercises(user_id)]
    assert len(names) == len({n.lower() for n in names})


async def test_renaming_to_its_own_name_is_allowed(fresh_db, user_id):
    """Иначе смена регистра в собственном названии была бы невозможна."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "жим лёжа", group_id)

    assert await db.update_exercise_name(ex_id, "Жим лёжа") is True
    assert (await db.get_exercise(ex_id))["display_name"] == "Жим лёжа"


async def test_a_clash_with_another_users_exercise_is_not_a_clash(fresh_db, user_id):
    db = fresh_db
    await db.get_or_create_user(telegram_id=222, username="other")
    g1 = await db.create_muscle_group(user_id, "Грудь")
    g2 = await db.create_muscle_group(222, "Грудь")
    await db.create_exercise(222, "Жим лёжа", g2)
    mine = await db.create_exercise(user_id, "Разводка", g1)

    assert await db.update_exercise_name(mine, "Жим лёжа") is True


async def test_a_clash_with_an_archived_exercise_is_reported(fresh_db, user_id):
    """Иначе переименование «проходит», а списки показывают одно имя дважды,
    как только архивное вернут."""
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    archived = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.archive_exercise(archived)
    other = await db.create_exercise(user_id, "Разводка", group_id)

    assert await db.update_exercise_name(other, "Жим лёжа") is False


# ---------- E4: возврат из архива проговаривается ----------


async def test_creating_an_exercise_that_exists_archived_says_so(fresh_db, user_id):
    """create_exercise намеренно переиспользует архивную строку (иначе история
    разошлась бы), но молча это выглядит как «новое» упражнение с чужим прошлым."""

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    old = await db.create_exercise(user_id, "Жим лёжа", group_id)
    await db.archive_exercise(old)

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.update_data(exm_group_id=group_id)
    answerer = MagicMock()
    answerer.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))

    await exercises._exm_finish_new_exercise_name(answerer, state, user_id, "Жим лёжа")

    text = answerer.answer.await_args.args[0]
    assert "вернул из архива" in text
    assert (await db.get_exercise(old))["is_archived"] == 0


async def test_creating_a_genuinely_new_exercise_says_nothing_extra(fresh_db, user_id):

    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.update_data(exm_group_id=group_id)
    answerer = MagicMock()
    answerer.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))

    await exercises._exm_finish_new_exercise_name(answerer, state, user_id, "Жим лёжа")

    assert "вернул из архива" not in answerer.answer.await_args.args[0]


# ---------- E8: импорт видит каталог ----------


async def test_the_import_resolver_offers_a_matching_catalog_template(fresh_db, user_id):
    """Импорт заводит упражнения десятками, и совпавшее с каталогом имя
    приезжало голым — без техники, фото и группы."""
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    event = MagicMock()
    event.from_user = SimpleNamespace(id=user_id, username="tester")
    event.answer = AsyncMock()

    await exercise_resolve.start(event, state, ["Жим штанги лёжа"])

    kb = event.answer.await_args.kwargs["reply_markup"]
    buttons = [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]
    template_rows = [b for b in buttons if b[1].startswith("resolve:tpl:")]
    assert template_rows, buttons
    assert "Жим штанги лёжа" in template_rows[0][0]


async def test_taking_the_template_forks_it_with_its_assets(fresh_db, user_id):

    db = fresh_db
    template = next(
        t for t in await db.list_all_exercise_templates() if t["name"] == "Жим штанги лёжа"
    )
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.update_data(
        resolve_pending=["Жим штанги лёжа"], resolve_resolved={}, resolve_total=1,
        resolve_current_name="Жим штанги лёжа",
    )
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.data = f"resolve:tpl:{template['id']}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()

    # Резолвер по завершении отдаёт управление импортёру — в тесте он не нужен.
    import handlers.csv_import as csv_import
    csv_import.on_exercises_resolved = AsyncMock()

    await exercise_resolve.resolve_pick_template(callback, state)

    mine = await db.list_user_exercises(user_id)
    assert [e["display_name"] for e in mine] == ["Жим штанги лёжа"]
    assert exercise_descriptions.effective_description(mine[0])
    assert len(exercise_media.get_images_for(mine[0])) == 2
