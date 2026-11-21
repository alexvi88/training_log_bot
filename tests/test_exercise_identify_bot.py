"""«Что это за тренажёр» в боте: фото в подборе упражнения → ответ тренера с
кнопками → выбор ведёт ровно туда же, куда тап по упражнению в списке."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import ai_trainer
import exercise_identify
from handlers import ai_trainer as ai_trainer_handlers
from handlers import workout
from tests.test_workout_picker import _make_callback, _make_message, _make_state


def _verdict(**overrides):
    verdict = {
        "unclear": False, "catalog_name": None, "own_name": None, "alternatives": [],
        "new_name": None, "new_group": None, "equipment": None,
        "title": "Это гак-машина.", "tip": "Спину прижми к спинке.",
    }
    verdict.update(overrides)
    return verdict


def _install(monkeypatch, verdict):
    calls = []

    async def fake_verdict(user_id, image_data_url, own_names, *, on_block=None):
        calls.append(image_data_url)
        return verdict

    async def fake_download(message):
        return "data:image/jpeg;base64,AAAA"

    monkeypatch.setattr(exercise_identify, "_model_verdict", fake_verdict)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer_handlers, "_download_photo_as_data_url", fake_download)
    chosen = []

    async def fake_chosen(event, state, ex_id):
        chosen.append(ex_id)

    monkeypatch.setattr(workout, "_on_exercise_chosen", fake_chosen)
    return calls, chosen


def _photo_message(user_id):
    message = _make_message(user_id, None)
    message.photo = [MagicMock()]
    placeholder = MagicMock()
    placeholder.delete = AsyncMock()
    message.reply = AsyncMock(return_value=placeholder)
    return message


def _tap(user_id, data):
    callback = _make_callback(user_id, data)
    callback.message = MagicMock()
    callback.message.delete = AsyncMock()
    return callback


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_photo_in_picker_offers_catalog_and_logs_into_it(fresh_db, user_id, monkeypatch):
    calls, chosen = _install(monkeypatch, _verdict(catalog_name="Гак-присед", alternatives=["Жим ногами"]))
    state = await _make_state(user_id)
    message = _photo_message(user_id)
    await workout.pick_exercise_by_photo(message, state)

    assert calls == ["data:image/jpeg;base64,AAAA"]
    answer = message.reply.await_args_list[-1]
    assert answer.args[0] == "Это гак-машина.\nСпину прижми к спинке."
    buttons = _buttons(answer.kwargs["reply_markup"])
    assert buttons == [("▶ Записать сюда", "pick:idf:opt:0"), ("Или: Жим ногами", "pick:idf:opt:1")]
    assert ai_trainer_handlers._busy == set()

    callback = _tap(user_id, "pick:idf:opt:0")
    await workout.pick_identified_exercise(callback, state)
    fork = await fresh_db.find_exercise_by_original_name(user_id, "Гак-присед")
    assert fork is not None and chosen == [fork["id"]]
    callback.message.delete.assert_awaited()

    # Повтор того же фото и того же выбора — то же упражнение, без дубля.
    await workout.pick_exercise_by_photo(_photo_message(user_id), state)
    await workout.pick_identified_exercise(_tap(user_id, "pick:idf:opt:0"), state)
    assert chosen == [fork["id"], fork["id"]]


async def test_photo_in_picker_creates_new_exercise(fresh_db, user_id, monkeypatch):
    _calls, chosen = _install(monkeypatch, _verdict(new_name="Маятниковый присед", new_group="legs", equipment="machine"))
    state = await _make_state(user_id)
    message = _photo_message(user_id)
    await workout.pick_exercise_by_photo(message, state)
    buttons = _buttons(message.reply.await_args_list[-1].kwargs["reply_markup"])
    assert buttons == [("➕ Создать «Маятниковый присед»", "pick:idf:new")]
    await workout.pick_identified_exercise(_tap(user_id, "pick:idf:new"), state)
    row = await fresh_db.find_exercise_by_name(user_id, "Маятниковый присед")
    assert row is not None and chosen == [row["id"]]


async def test_unclear_photo_has_no_buttons_and_stale_tap_is_answered(fresh_db, user_id, monkeypatch):
    _calls, chosen = _install(monkeypatch, _verdict(unclear=True))
    state = await _make_state(user_id)
    message = _photo_message(user_id)
    await workout.pick_exercise_by_photo(message, state)
    answer = message.reply.await_args_list[-1]
    assert answer.args[0] == "Не разобрал тренажёр. Сними его целиком, сбоку."
    assert answer.kwargs["reply_markup"] is None
    callback = _tap(user_id, "pick:idf:opt:0")
    await workout.pick_identified_exercise(callback, state)
    assert chosen == []
    callback.answer.assert_awaited()
