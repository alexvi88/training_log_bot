"""«Чем заменить» — альтернативы упражнению из каталога (exercise_alternatives):
данные сверены с каталогом, связь симметрична, экран бота и ручка /v1 отдают
свою копию там, где она уже есть, и не протекают русским англоязычному."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import exercise_alternatives
import i18n
import i18n_coverage
import seed_data
from fsm import ExerciseManage
from handlers import exercises as exercises_handlers

_CATALOG = {name for _group, name in seed_data.EXERCISE_TEMPLATES}


def test_every_family_name_is_a_catalog_template():
    for family in exercise_alternatives.FAMILIES:
        assert len(family) >= 2, family
        for name in family:
            assert name in _CATALOG, f"{name!r} нет в EXERCISE_TEMPLATES"


def test_alternatives_are_symmetric_and_exclude_self():
    for name in _CATALOG:
        alts = exercise_alternatives.alternatives_for(name)
        assert name not in alts
        assert len(alts) == len(set(alts))
        assert len(alts) <= exercise_alternatives.MAX_ALTERNATIVES
        for alt in alts:
            # Обрезка по MAX_ALTERNATIVES может съесть обратную связь у
            # большого семейства — симметрию проверяем на полном списке.
            full = {n for f in exercise_alternatives.FAMILIES if alt in f for n in f}
            assert name in full, (name, alt)


def test_staple_lifts_have_alternatives():
    for name in ("Жим штанги лёжа", "Присед со штангой", "Становая тяга",
                 "Тяга верхнего блока", "Разгибание на трицепс на блоке"):
        assert exercise_alternatives.alternatives_for(name), name


def test_own_exercise_has_no_alternatives():
    assert exercise_alternatives.alternatives_for("Моё странное упражнение") == []


async def _fork(db, user_id, canonical):
    template = (await db.find_global_templates_by_names([canonical]))[canonical]
    return await db.fork_exercise_from_template(user_id, template["id"])


async def test_for_exercise_points_to_own_copy_when_present(fresh_db, user_id):
    db = fresh_db
    bench_id = await _fork(db, user_id, "Жим штанги лёжа")
    db_bench_id = await _fork(db, user_id, "Жим гантелей лёжа")
    bench = await db.get_exercise(bench_id)
    with i18n.use_lang("ru"):
        alts = await exercise_alternatives.for_exercise(user_id, bench, "ru")
    by_name = {a["name"]: a for a in alts}
    assert by_name["Жим гантелей лёжа"]["exercise_id"] == db_bench_id
    assert by_name["Жим в тренажёре"]["exercise_id"] is None
    assert by_name["Жим в тренажёре"]["template_id"]


async def test_archived_copy_is_offered_as_template(fresh_db, user_id):
    db = fresh_db
    bench_id = await _fork(db, user_id, "Жим штанги лёжа")
    db_bench_id = await _fork(db, user_id, "Жим гантелей лёжа")
    await db.archive_exercise(db_bench_id)
    alts = await exercise_alternatives.for_exercise(user_id, await db.get_exercise(bench_id), "ru")
    assert all(a["exercise_id"] != db_bench_id for a in alts)


def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _callback(user_id: int, data: str) -> CallbackQuery:
    message = MagicMock()
    message.text = "card"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.bot = MagicMock()
    callback.bot.delete_message = AsyncMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _sent(callback) -> tuple[str, list]:
    """Текст и кнопки экрана — как бы он ни ушёл: правкой или новым сообщением."""
    for mock in (callback.message.edit_text, callback.message.answer):
        if mock.await_args:
            args, kwargs = mock.await_args
            text = kwargs.get("text", args[0] if args else "")
            kb = kwargs.get("reply_markup")
            return text, [b for row in kb.inline_keyboard for b in row]
    raise AssertionError("экран не отправлен")


async def test_card_button_and_screen_in_english(fresh_db, user_id):
    db = fresh_db
    await db.set_user_lang(user_id, "en")
    with i18n.use_lang("en"):
        bench_id = await _fork(db, user_id, "Жим штанги лёжа")
        bench = await db.get_exercise(bench_id)
        _text, kb = exercises_handlers._exercise_detail_view(bench)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"exm:alts:{bench_id}" in datas

        state = _state(user_id)
        await state.set_state(ExerciseManage.picking_exercise)
        callback = _callback(user_id, f"exm:alts:{bench_id}")
        await exercises_handlers.exm_alternatives(callback, state)
    text, buttons = _sent(callback)
    shown = text + "\n" + "\n".join(b.text for b in buttons)
    assert not i18n_coverage.has_cyrillic(shown), shown
    assert any(b.callback_data.startswith("exm:altpv:") for b in buttons)
    assert buttons[-1].callback_data == f"exm:ex:{bench_id}"


async def test_own_exercise_card_has_no_button(fresh_db, user_id):
    db = fresh_db
    ex_id = await db.create_exercise(user_id, "Моё упражнение", None, None, False, None)
    _text, kb = exercises_handlers._exercise_detail_view(await db.get_exercise(ex_id))
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(d.startswith("exm:alts:") for d in datas)
