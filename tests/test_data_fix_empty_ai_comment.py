"""Пустой ответ модели на комментарий к тренировке больше не сохраняется
заглушкой (ai_trainer.EmptyComment), а разовая правка
data_fixes.EMPTY_AI_COMMENT_STUB снимает заглушки, уже лежащие в базе."""

from unittest.mock import AsyncMock

import pytest

import ai_trainer
import data_fixes
import i18n
from tests.luna_fakes import as_luna
from tests.test_ai_trainer import _fake_client, _response

pytestmark = pytest.mark.asyncio


async def _finished_workout(db, user_id: int) -> int:
    await db.get_or_create_user(telegram_id=user_id, username="tester")
    workout_id = await db.create_workout(user_id)
    await db.finish_workout(workout_id)
    return workout_id


async def test_stub_is_cleared_and_real_comments_stay(fresh_db):
    db = fresh_db
    stub_id = await _finished_workout(db, 111)
    real_id = await _finished_workout(db, 222)
    await db.set_workout_ai_comment(stub_id, data_fixes._EMPTY_AI_COMMENT_STUB_TEXT)
    await db.set_workout_ai_comment(real_id, "Хорошая тренировка, жим подрос.")

    await data_fixes.run_pending()

    assert (await db.get_workout(stub_id))["ai_comment"] is None
    assert (await db.get_workout(real_id))["ai_comment"] == "Хорошая тренировка, жим подрос."
    assert await db.is_data_fix_done(data_fixes.EMPTY_AI_COMMENT_STUB)


async def test_empty_model_comment_raises_instead_of_a_stub(fresh_db, monkeypatch):
    workout_id = await _finished_workout(fresh_db, 111)
    client = _fake_client([_response(content="   ")])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(client))

    with pytest.raises(ai_trainer.EmptyComment):
        await ai_trainer.comment_on_workout(111, workout_id)


async def test_empty_model_comment_is_not_saved(fresh_db, monkeypatch):
    db = fresh_db
    workout_id = await _finished_workout(db, 111)
    user = await db.get_user(111)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "comment_on_workout", AsyncMock(side_effect=ai_trainer.EmptyComment("x")))
    await db.conn().execute("UPDATE users SET ai_comments_enabled = 1 WHERE telegram_id = 111")
    await db.conn().commit()
    user = await db.get_user(111)

    assert await ai_trainer.ensure_workout_comment(user, workout_id) is None
    assert (await db.get_workout(workout_id))["ai_comment"] is None


async def test_empty_chat_answer_speaks_the_athletes_language():
    with i18n.use_lang("en"):
        assert i18n.t("ai.screen.empty_answer") == "Couldn't put an answer together — ask me again."
