"""Тренер, который назвал программу, должен на неё же и сослаться — и уметь
предложить её удалить, не удаляя сам.

Раньше на «удали вторую Вику» бот отвечал «удалять программы я не умею — иди в
⚙️ Программы», а на «скинь ссылку на прогу» — «такого у меня нет»: ответ знал,
о какой программе речь, а кнопка под ним об этом не знала.
"""

import pytest

import ai_trainer
import db as dbmod
import keyboards
import program_mentions

pytestmark = pytest.mark.asyncio


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


async def _saved(db, user_id: int) -> tuple[int, int]:
    """Многодневка «Вика: ноги/верх» и одиночная «Домашка на турнике»."""
    program_id = await db.create_program(user_id, "Вика: ноги/верх")
    await db.create_routine(user_id, "Ноги", program_id=program_id)
    await db.create_routine(user_id, "Верх", program_id=program_id)
    solo_id = await db.create_routine(user_id, "Домашка на турнике")
    return program_id, solo_id


# ---------- ссылки на программы под ответом ----------


async def test_named_program_becomes_a_link(fresh_db, user_id):
    db = fresh_db
    program_id, solo_id = await _saved(db, user_id)

    found = await program_mentions.find_in_text(
        user_id, "По «Вике: ноги/верх» ты ходишь стабильно, а домашку на турнике забросил."
    )

    assert [(t["kind"], t["id"]) for t in found] == [("program", program_id), ("routine", solo_id)]


async def test_program_not_named_gets_no_button(fresh_db, user_id):
    db = fresh_db
    await _saved(db, user_id)
    assert await program_mentions.find_in_text(user_id, "Сегодня отдыхай, завтра ноги.") == []


async def test_a_name_of_only_common_words_is_not_a_link(fresh_db, user_id):
    """«Программа от 02.08» иначе ловилась бы в любом абзаце со словом
    «программа» — ссылка не на ту программу хуже, чем её отсутствие."""
    db = fresh_db
    program_id = await db.create_program(user_id, "Программа")
    await db.create_routine(user_id, "День 1", program_id=program_id)

    assert await program_mentions.find_in_text(user_id, "Твоя программа выглядит нормально.") == []


async def test_multiday_and_solo_open_their_own_screens():
    kb = keyboards.ai_trainer_keyboard(
        programs=[
            {"kind": "program", "id": 7, "name": "Вика: ноги/верх"},
            {"kind": "routine", "id": 3, "name": "Домашка"},
        ],
    )
    assert _callbacks(kb)[:2] == ["rt:prg:7", "rt:view:3"]
    assert _labels(kb)[:2] == ["🗂 Вика: ноги/верх", "🗂 Домашка"]


async def test_programs_share_pagination_with_exercises():
    """Место под ответом одно: программа не должна занимать строку сверх лимита."""
    exercises = [{"id": i, "display_name": f"Упражнение {i}", "is_template": False} for i in range(1, 4)]
    kb = keyboards.ai_trainer_keyboard(
        programs=[{"kind": "program", "id": 7, "name": "Вика"}], exercises=exercises,
    )
    shown = _callbacks(kb)
    assert shown[: keyboards.AI_MENTION_PAGE_SIZE] == ["rt:prg:7", "ai:excard:1", "ai:excard:2"]
    # Стрелка несёт и программу, и упражнения — по префиксу видно, что есть что.
    assert "ai:mpage:1:p7,1,2,3" in shown


# ---------- предложение удалить ----------


async def test_delete_program_only_proposes(fresh_db, user_id):
    db = fresh_db
    program_id, _ = await _saved(db, user_id)

    payload, action = await ai_trainer._delete_program(user_id, {"name": "Вика: ноги/верх"})

    assert action == {
        "label": "🗑 Удалить: Вика: ноги/верх",
        "callback": f"rt:pgmdelask:{program_id}",
    }
    assert payload["ok"] is True
    assert "НЕ УДАЛЕНО" in payload["note"]
    # Ничего не снесено: сносит тап пользователя на экране подтверждения.
    assert await dbmod.get_program(program_id) is not None


async def test_delete_program_on_a_wrong_name_lists_the_real_ones(fresh_db, user_id):
    db = fresh_db
    await _saved(db, user_id)

    payload, action = await ai_trainer._delete_program(user_id, {"name": "Вика 2"})

    assert action is None
    assert "нет" in payload["error"]
    assert set(payload["saved_programs"]) == {"Вика: ноги/верх", "Домашка на турнике"}


async def test_a_solo_program_is_deleted_through_its_own_screen(fresh_db, user_id):
    db = fresh_db
    _, solo_id = await _saved(db, user_id)

    _, action = await ai_trainer._delete_program(user_id, {"name": "Домашка на турнике"})

    assert action["callback"] == f"rt:delask:{solo_id}"


async def test_proposed_actions_become_buttons_above_the_mentions():
    kb = keyboards.ai_trainer_keyboard(
        actions=[{"label": "🗑 Удалить: Вика", "callback": "rt:pgmdelask:7"}],
        programs=[{"kind": "program", "id": 9, "name": "PPL"}],
    )
    assert _callbacks(kb)[:2] == ["rt:pgmdelask:7", "rt:prg:9"]
    assert _labels(kb)[0] == "🗑 Удалить: Вика"


async def test_action_is_reachable_through_the_tool_dispatcher(fresh_db, user_id):
    """execute_tool отдаёт действие наружу колбэком — иначе кнопку не на что вешать."""
    db = fresh_db
    program_id, _ = await _saved(db, user_id)
    seen: list[dict] = []

    async def collect(action):
        seen.append(action)

    await ai_trainer.execute_tool(
        user_id, "delete_program", {"name": "Вика: ноги/верх"}, on_action=collect
    )

    assert seen == [
        {"label": "🗑 Удалить: Вика: ноги/верх", "callback": f"rt:pgmdelask:{program_id}"}
    ]


# ---------- экраны подтверждения для необратимого ----------


def _make_callback(user_id: int, data: str):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _state(user_id: int):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def test_merge_button_asks_before_merging(fresh_db, user_id):
    """Разобрать слитые программы обратно UI не умеет — вопрос обязателен."""
    from handlers import ai_trainer as handler

    db = fresh_db
    source = await db.create_program(user_id, "Вика")
    await db.create_routine(user_id, "Ноги", program_id=source)
    target = await db.create_program(user_id, "Вика (2)")
    await db.create_routine(user_id, "Верх", program_id=target)

    cb = _make_callback(user_id, f"ai:pgmmergeask:{source}:{target}")
    await handler.ai_program_merge_confirm(cb, await _state(user_id))

    kwargs = cb.message.answer.await_args.kwargs
    assert [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row] == [
        f"rt:pgmmerge:{source}:{target}", "ai:menu",
    ]
    # Пока не подтвердили — обе программы на месте.
    assert await dbmod.get_program(source) is not None


async def test_archive_button_asks_and_then_archives(fresh_db, user_id):
    from handlers import ai_trainer as handler

    db = fresh_db
    group = await db.create_muscle_group(user_id, "Другое")
    fly = await db.create_exercise(user_id, "Сведения", group)
    state = await _state(user_id)

    await handler.ai_exercise_archive_confirm(_make_callback(user_id, f"ai:exarchask:{fly}"), state)
    assert (await dbmod.get_exercise(fly))["is_archived"] == 0

    await handler.ai_exercise_archive(_make_callback(user_id, f"ai:exarchyes:{fly}"), state)
    assert (await dbmod.get_exercise(fly))["is_archived"] == 1


async def test_archive_refuses_someone_elses_exercise(fresh_db, user_id):
    from handlers import ai_trainer as handler

    db = fresh_db
    other = await db.get_or_create_user(telegram_id=222, username="other")
    group = await db.create_muscle_group(other["telegram_id"], "Другое")
    theirs = await db.create_exercise(other["telegram_id"], "Сведения", group)

    cb = _make_callback(user_id, f"ai:exarchyes:{theirs}")
    await handler.ai_exercise_archive(cb, await _state(user_id))

    cb.answer.assert_awaited_once_with("Не нашёл это упражнение — экран устарел. Вернись назад и открой заново", show_alert=True)
    assert (await dbmod.get_exercise(theirs))["is_archived"] == 0


async def test_archive_multi_button_asks_names_and_then_archives_all(fresh_db, user_id):
    """Живой репорт: массовая архивация ставила под ответом кнопку на каждое
    упражнение — теперь одна кнопка на все, но экран подтверждения обязан
    называть их поимённо, не только числом."""
    from handlers import ai_trainer as handler

    db = fresh_db
    group = await db.create_muscle_group(user_id, "Другое")
    fly = await db.create_exercise(user_id, "Сведения", group)
    curl = await db.create_exercise(user_id, "Подъём на бицепс", group)
    state = await _state(user_id)
    await state.update_data(ai_archive={"a1": [fly, curl]})

    ask = _make_callback(user_id, "ai:exarchaskmulti:a1")
    await handler.ai_exercise_archive_confirm_multi(ask, state)

    text = ask.message.answer.await_args.args[0]
    assert "Сведения" in text and "Подъём на бицепс" in text
    kb = ask.message.answer.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [
        "ai:exarchyesmulti:a1", "ai:menu",
    ]
    assert (await dbmod.get_exercise(fly))["is_archived"] == 0

    yes = _make_callback(user_id, "ai:exarchyesmulti:a1")
    await handler.ai_exercise_archive_multi(yes, state)

    assert (await dbmod.get_exercise(fly))["is_archived"] == 1
    assert (await dbmod.get_exercise(curl))["is_archived"] == 1
    done_text = yes.message.edit_text.await_args.args[0]
    assert "Сведения" in done_text and "Подъём на бицепс" in done_text


async def test_archive_multi_skips_ids_from_someone_else(fresh_db, user_id):
    from handlers import ai_trainer as handler

    db = fresh_db
    other = await db.get_or_create_user(telegram_id=223, username="other2")
    their_group = await db.create_muscle_group(other["telegram_id"], "Другое")
    theirs = await db.create_exercise(other["telegram_id"], "Чужое", their_group)
    group = await db.create_muscle_group(user_id, "Другое")
    mine = await db.create_exercise(user_id, "Моё", group)
    state = await _state(user_id)
    await state.update_data(ai_archive={"a1": [theirs, mine]})

    await handler.ai_exercise_archive_multi(_make_callback(user_id, "ai:exarchyesmulti:a1"), state)

    assert (await dbmod.get_exercise(theirs))["is_archived"] == 0
    assert (await dbmod.get_exercise(mine))["is_archived"] == 1
