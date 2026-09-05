"""Кнопка «💬 Чат атлетов» в главном меню появляется не сразу (см.
handlers.workout._main_menu_kb, config.COMMUNITY_MIN_FINISHED_WORKOUTS): с
пустым дневником в общий чат взрослых атлетов звать рано. /community при этом
работает как раньше для всех (handlers/community.py) — порог только у кнопки в
меню."""

import config
from handlers import workout

CHAT_URL = "https://t.me/example"


async def _finish_workout(db, user_id: int) -> None:
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 5, None)
    await db.finish_workout(workout_id)


def _button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_community_button_hidden_before_threshold(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", CHAT_URL)
    db = fresh_db

    for _ in range(config.COMMUNITY_MIN_FINISHED_WORKOUTS - 1):
        await _finish_workout(db, user_id)

    markup = await workout._main_menu_kb(user_id, active=None)
    assert not any("Чат атлетов" in text for text in _button_texts(markup))


async def test_community_button_shown_at_threshold(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", CHAT_URL)
    db = fresh_db

    for _ in range(config.COMMUNITY_MIN_FINISHED_WORKOUTS):
        await _finish_workout(db, user_id)

    markup = await workout._main_menu_kb(user_id, active=None)
    (button,) = [b for row in markup.inline_keyboard for b in row if "Чат атлетов" in b.text]
    assert button.url == CHAT_URL


async def test_community_button_hidden_without_url_even_past_threshold(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(config, "COMMUNITY_CHAT_URL", "")
    db = fresh_db

    for _ in range(config.COMMUNITY_MIN_FINISHED_WORKOUTS):
        await _finish_workout(db, user_id)

    markup = await workout._main_menu_kb(user_id, active=None)
    assert not any("Чат атлетов" in text for text in _button_texts(markup))
