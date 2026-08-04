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

    payload, target = await ai_trainer._delete_program(user_id, {"name": "Вика: ноги/верх"})

    assert target == {"kind": "program", "id": program_id, "name": "Вика: ноги/верх"}
    assert payload["ok"] is True
    assert "НЕ УДАЛЕНО" in payload["note"]
    # Ничего не снесено: сносит тап пользователя на экране подтверждения.
    assert await dbmod.get_program(program_id) is not None


async def test_delete_program_on_a_wrong_name_lists_the_real_ones(fresh_db, user_id):
    db = fresh_db
    await _saved(db, user_id)

    payload, target = await ai_trainer._delete_program(user_id, {"name": "Вика 2"})

    assert target is None
    assert "нет" in payload["error"]
    assert set(payload["saved_programs"]) == {"Вика: ноги/верх", "Домашка на турнике"}


async def test_delete_button_leads_to_the_usual_confirmation():
    program = keyboards.ai_trainer_keyboard(delete_target={"kind": "program", "id": 7, "name": "Вика"})
    solo = keyboards.ai_trainer_keyboard(delete_target={"kind": "routine", "id": 3, "name": "Домашка"})

    assert _callbacks(program)[0] == "rt:pgmdelask:7"
    assert _labels(program)[0] == "🗑 Удалить: Вика"
    assert _callbacks(solo)[0] == "rt:delask:3"


async def test_delete_target_is_reachable_through_the_tool_dispatcher(fresh_db, user_id):
    """execute_tool отдаёт цель наружу колбэком — иначе кнопку не на что вешать."""
    db = fresh_db
    program_id, _ = await _saved(db, user_id)
    seen: list[dict] = []

    async def collect(target):
        seen.append(target)

    await ai_trainer.execute_tool(
        user_id, "delete_program", {"name": "Вика: ноги/верх"}, on_delete=collect
    )

    assert seen == [{"kind": "program", "id": program_id, "name": "Вика: ноги/верх"}]
