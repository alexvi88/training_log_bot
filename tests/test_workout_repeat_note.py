"""The one-tap "🔁 Повторить" set copier and the per-exercise 📝 note flow on
the live logging screen."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer
import keyboards
from fsm import WorkoutFlow
from handlers import workout


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.chat = SimpleNamespace(id=user_id)
    next_answer_id = iter(range(600, 700))

    async def _answer(*args, **kwargs):
        return SimpleNamespace(message_id=next(next_answer_id), chat=SimpleNamespace(id=user_id))

    message.answer = AsyncMock(side_effect=_answer)
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock(side_effect=_answer)
    message.bot = bot
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str, message_id: int = 55):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_id = message_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester")
    msg.text = text
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    bot = MagicMock()
    bot.delete_message = AsyncMock()
    bot.set_message_reaction = AsyncMock()

    async def _send(*args, **kwargs):
        return SimpleNamespace(message_id=700, chat=SimpleNamespace(id=user_id))

    bot.send_message = AsyncMock(side_effect=_send)
    msg.bot = bot
    return msg


async def _setup_logging(db, user_id: int):
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, 100.0, 8, None)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
        last_by_exercise={ex_id: (100.0, 8)}, last_session_sets={},
    )
    return state, ex_id, block_id, workout_id


@pytest.mark.asyncio
async def test_repeat_copies_last_set(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)
    callback = _make_callback(user_id, "live:repeat")

    await workout.live_repeat_set(callback, state)

    sets = await db.list_sets_for_block(block_id)
    assert len(sets) == 2
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (100.0, 8)


@pytest.mark.asyncio
async def test_repeat_with_no_sets_is_a_noop(fresh_db, user_id):
    db = fresh_db
    group_id = await db.create_muscle_group(user_id, "Грудь")
    ex_id = await db.create_exercise(user_id, "Жим лёжа", group_id)
    workout_id = await db.create_workout(user_id)
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, ex_id, 0)

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(WorkoutFlow.logging_set)
    await state.update_data(
        workout_id=workout_id, live_chat_id=user_id, live_message_id=42,
        open_exercises=[ex_id], open_blocks={ex_id: block_id}, active_exercise_id=ex_id,
    )
    callback = _make_callback(user_id, "live:repeat")

    await workout.live_repeat_set(callback, state)

    assert await db.list_sets_for_block(block_id) == []
    callback.answer.assert_awaited_with("Нет подхода для повтора")


@pytest.mark.asyncio
async def test_note_entered_saves_to_workout_exercise(fresh_db, user_id):
    db = fresh_db
    state, ex_id, _, workout_id = await _setup_logging(db, user_id)
    await state.set_state(WorkoutFlow.logging_exercise_note)
    message = _make_message(user_id, "болит плечо — следи за локтями")

    await workout.live_note_entered(message, state)

    note = await db.get_workout_exercise_note(workout_id, ex_id)
    assert note == "болит плечо — следи за локтями"
    assert await state.get_state() == WorkoutFlow.logging_set


@pytest.mark.asyncio
async def test_note_dash_clears_an_existing_note(fresh_db, user_id):
    db = fresh_db
    state, ex_id, _, workout_id = await _setup_logging(db, user_id)
    await db.set_workout_exercise_note(workout_id, ex_id, "болит плечо")
    await state.set_state(WorkoutFlow.logging_exercise_note)
    message = _make_message(user_id, "-")

    await workout.live_note_entered(message, state)

    note = await db.get_workout_exercise_note(workout_id, ex_id)
    assert note is None


async def _finished_baseline(db, user_id, ex_id, weight, reps):
    """A prior finished workout with one set, so later PR detection has history."""
    wid = await db.create_workout(user_id)
    block_id = await db.create_block(wid, "single")
    await db.add_block_exercise(block_id, ex_id, 0)
    await db.add_set(block_id, ex_id, 0, 0, weight, reps, None)
    await db.finish_workout(wid, None, finished_at="2020-01-01T12:00:05")
    # Backdate so it sorts before the active workout.
    await db.update_workout_date(wid, "2020-01-01T12:00:00", "2020-01-01T12:00:05")


@pytest.mark.asyncio
async def test_record_set_reacts_and_keeps_message_briefly(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 100.0, 5)
    message = _make_message(user_id, "150 5")  # clear e1RM record

    await workout.log_set_text(message, state)

    message.bot.set_message_reaction.assert_awaited_once()
    react = message.bot.set_message_reaction.await_args.kwargs["reaction"]
    assert react[0].emoji == "🔥"
    message.delete.assert_not_awaited()  # not tidied away immediately, unlike a normal set


@pytest.mark.asyncio
async def test_record_set_message_is_deleted_after_delay(fresh_db, user_id, monkeypatch):
    """The 🔥 reaction message isn't left in the chat forever — it's cleaned up
    after a delay like everything else, just not instantly (so it can be noticed)."""
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 100.0, 5)
    message = _make_message(user_id, "150 5")  # clear e1RM record

    monkeypatch.setattr(workout, "_RECORD_MESSAGE_LIFETIME_SECONDS", 0)
    scheduled = []
    # _spawn is the seam for background work now (it keeps a strong reference
    # so the task cannot be collected mid-flight); capture instead of running.
    monkeypatch.setattr(workout, "_spawn", lambda coro: scheduled.append(coro))

    await workout.log_set_text(message, state)

    # log_set_text also re-renders the live tracker (delete-and-resend), which
    # uses this same bot.delete_message mock for an unrelated message — count
    # calls rather than asserting zero.
    assert len(scheduled) == 1
    calls_before = message.bot.delete_message.await_count

    await scheduled[0]  # let the delayed-delete task run to completion

    assert message.bot.delete_message.await_count == calls_before + 1
    message.bot.delete_message.assert_awaited_with(
        chat_id=message.chat.id, message_id=message.message_id
    )


@pytest.mark.asyncio
async def test_ordinary_set_is_deleted_without_reaction(fresh_db, user_id):
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)
    await _finished_baseline(db, user_id, ex_id, 200.0, 5)  # high baseline
    message = _make_message(user_id, "60 5")  # nowhere near a record

    await workout.log_set_text(message, state)

    message.bot.set_message_reaction.assert_not_awaited()
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_voice_logs_a_set(fresh_db, user_id, monkeypatch):
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)

    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "сто на восемь"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)

    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))

    await workout.log_set_voice(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (100.0, 8)
    assert "Записал" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_reply_target_gone_still_confirms_the_saved_set(fresh_db, user_id, monkeypatch):
    """Находка 5: if the voice message was deleted mid-transcription, `reply`
    (and a reply_to send_message) raise TelegramBadRequest — but the sets are
    already written by then, so the user must still get "Записал", via a
    plain send_message, instead of an unhandled crash reading as "что-то
    пошло не так" for a save that actually worked."""
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)

    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "сто на восемь"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)

    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))
    message.reply = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="message to reply not found")
    )

    await workout.log_set_voice(message, state)

    sets = await db.list_sets_for_block(block_id)
    assert (sets[-1]["weight"], sets[-1]["reps"]) == (100.0, 8)  # the set was saved
    message.reply.assert_awaited()
    # Fell back to a plain send_message instead of raising (the tracker
    # re-render sends its own message too, so scan every call rather than
    # assuming this is the last one).
    texts = [
        call.kwargs.get("text") or (call.args[1] if len(call.args) > 1 else None)
        for call in message.bot.send_message.await_args_list
    ]
    assert any(t and "Записал" in t for t in texts)


@pytest.mark.asyncio
async def test_voice_unparseable_asks_to_retry(fresh_db, user_id, monkeypatch):
    db = fresh_db
    state, ex_id, block_id, _ = await _setup_logging(db, user_id)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)

    async def _fake_transcribe(buf, uid):
        return "давай запиши что-нибудь"

    monkeypatch.setattr(ai_trainer, "transcribe_voice", _fake_transcribe)

    message = _make_message(user_id, text=None)
    message.voice = SimpleNamespace(file_id="v1", duration=2, file_size=1000)
    message.bot.download = AsyncMock(return_value=SimpleNamespace(name=""))

    await workout.log_set_voice(message, state)

    assert len(await db.list_sets_for_block(block_id)) == 1  # nothing new logged
    assert "Не понял" in message.reply.await_args.args[0]


def test_logging_keyboard_omits_repeat_but_keeps_note():
    """Кнопки повтора на экране нет — при живом обработчике live:repeat.

    Пробовали вернуть: в пару к «↩️ Удалить последний» она не встаёт (двадцать
    символов, в половинной колонке обрежется), а своей строкой удлиняет и без
    того высокий экран с вкладками. Повтор остаётся на «=» текстом.
    """
    for has_sets in (True, False):
        kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=has_sets)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "live:repeat" not in cbs
        assert "live:note:1" in cbs
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "live:undo" in cbs


def test_logging_keyboard_note_button_is_labelled():
    # A bare "📝" reads as "draft"/"edit" next to "➕ Суперсет" — it needs the word.
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "📝 Заметка" in texts


def test_logging_keyboard_puts_every_superset_tab_on_its_own_row():
    open_items = [(1, "Bench press"), (2, "Overhead press - machine"), (3, "Row")]
    kb = keyboards.logging_keyboard(open_items, active_id=1, has_sets=True)
    tab_rows = [
        row for row in kb.inline_keyboard
        if any(b.callback_data.startswith("live:switch:") for b in row)
    ]
    assert [len(r) for r in tab_rows] == [1, 1, 1]
    # A full-width row fits the whole name instead of cutting it to a stub.
    assert tab_rows[1][0].text == "Overhead press - machine"


def _tab_rows(kb):
    return [
        row for row in kb.inline_keyboard
        if any(b.callback_data.startswith("live:switch:") for b in row)
    ]


def test_superset_tabs_show_full_name_falling_back_to_wide_rows():
    # Tabs show the full exercise name (not just the "base" before a qualifier),
    # cutting into it only when it doesn't even fit a full-width tab.
    open_items = [
        (1, "biceps curls - alternating dumbbells"),
        (2, "triceps block - single arm - cuff"),
    ]
    kb = keyboards.logging_keyboard(open_items, active_id=2, has_sets=True)
    texts = [b.text for row in _tab_rows(kb) for b in row]
    assert texts == ["biceps curls - alternating…", "▶ triceps block - single…"]


def test_superset_of_two_gets_a_row_each():
    long_pair = [(1, "Подтягивания с весом"), (2, "Тяга")]
    rows = _tab_rows(keyboards.logging_keyboard(long_pair, active_id=1, has_sets=True))
    assert [len(r) for r in rows] == [1, 1]
    assert rows[0][0].text == "▶ Подтягивания с весом"  # full name, no ellipsis

    # Short names get their own row too — a shared row is what used to cut them.
    short_pair = [(1, "Bench"), (2, "Row")]
    assert [len(r) for r in _tab_rows(
        keyboards.logging_keyboard(short_pair, active_id=1, has_sets=True)
    )] == [1, 1]


def test_tab_label_cuts_on_a_word_boundary():
    assert keyboards._tab_label("Жим ногами в тренажёре", 13) == "Жим ногами…"
    # ...but not when that would leave almost nothing.
    assert keyboards._tab_label("Гиперэкстензия обратная", 13) == "Гиперэкстензи…"


def test_suspicious_weight_warning_flags_likely_typo():
    last_session = [(140.0, 6, None), (130.0, 8, None)]
    warning = workout._suspicious_weight_warning(last_session, today_sets=[(1.0, 1)])
    assert warning is not None
    assert "1кг?" in warning
    assert "140кг" in warning


def test_suspicious_weight_warning_silent_for_real_backoff_set():
    last_session = [(140.0, 6, None)]
    # 70kg is a plausible deliberate backoff set, not a typo.
    assert workout._suspicious_weight_warning(last_session, today_sets=[(70.0, 8)]) is None


def test_suspicious_weight_warning_exempt_for_bodyweight():
    last_session = [(0.0, 12, None)]
    assert workout._suspicious_weight_warning(last_session, today_sets=[(0.0, 3)]) is None


def test_suspicious_weight_warning_none_without_history():
    assert workout._suspicious_weight_warning(None, today_sets=[(1.0, 1)]) is None
    assert workout._suspicious_weight_warning([(140.0, 6, None)], today_sets=None) is None


def test_suspicious_weight_warning_counts_todays_own_sets():
    """Живой репорт: восемь подходов по 200, потом «500 5» — и ни одного
    вопроса. Сегодняшние подходы не участвовали в сверке вовсе, хотя они и есть
    самое свежее свидетельство о правдоподобном весе."""
    today = [(200.0, 4)] * 8 + [(500.0, 5)]

    warning = workout._suspicious_weight_warning([(210.0, 4, None)], today_sets=today)

    assert warning is not None
    assert "500кг?" in warning


def test_suspicious_weight_warning_says_today_when_the_baseline_is_todays():
    """«в прошлый раз 200кг» под списком, где 200 стоит восемь раз сегодня,
    читалось бы как ошибка самого бота."""
    today = [(200.0, 4)] * 8 + [(500.0, 5)]

    warning = workout._suspicious_weight_warning([(90.0, 4, None)], today_sets=today)

    assert warning is not None
    assert "сегодня 200кг" in warning


def test_suspicious_weight_warning_works_on_an_exercise_without_history():
    """Первая тренировка нового упражнения: прошлого раза нет, но свои же
    подходы получасовой давности есть — и сверять есть с чем."""
    assert workout._suspicious_weight_warning(None, today_sets=[(60.0, 8), (500.0, 5)]) is not None
    assert workout._suspicious_weight_warning(None, today_sets=[(60.0, 8), (65.0, 8)]) is None


def test_suspicious_weight_warning_asks_at_double_the_working_weight():
    """Порог был 3× и пропускал 200 → 500. Прибавка вдвое за один подход не
    бывает настоящей тренировкой, а цена вопроса — один тап «Да»."""
    last_session = [(100.0, 6, None)]
    assert workout._suspicious_weight_warning(last_session, today_sets=[(201.0, 5)]) is not None
    assert workout._suspicious_weight_warning(last_session, today_sets=[(200.0, 5)]) is None


def test_suspicious_weight_warning_flags_an_extra_digit_too():
    """The check used to be one-directional — only a suspiciously *low* weight
    was flagged, so "1400" typed for "140" (parser.MAX_WEIGHT's 1500 ceiling
    doesn't catch this one, since 1400 is still a "plausible" absolute weight)
    passed through silently."""
    last_session = [(140.0, 6, None)]
    warning = workout._suspicious_weight_warning(last_session, today_sets=[(1400.0, 6)])
    assert warning is not None
    assert "1400кг?" in warning
    assert "140кг" in warning


def test_suspicious_weight_warning_silent_for_plausible_progression():
    """A real jump in working weight — not a typo — shouldn't get flagged just
    because it's well above last session's."""
    last_session = [(140.0, 6, None)]
    assert workout._suspicious_weight_warning(last_session, today_sets=[(200.0, 5)]) is None


# ---------- «тот же вес, другие повторы» ----------


def test_reps_window_centres_on_the_last_set():
    assert keyboards.reps_window(10) == [7, 8, 9, 10, 11, 12]
    # Запас вниз больше: следующий подход внутри упражнения чаще выходит хуже
    # предыдущего, а не лучше — усталость копится.
    assert keyboards.reps_window(10).index(10) == 3


def test_reps_window_keeps_six_buttons_from_four_reps_up():
    """Рабочий диапазон: три ниже, два выше, всегда шесть кнопок."""
    for last in (4, 5, 7, 8, 10, 12, 20):
        window = keyboards.reps_window(last)
        assert window == list(range(last - 3, last + 3)), last
        assert len(window) == 6


def test_reps_window_slides_up_instead_of_shrinking_near_one():
    """После двойки нужны «1 2 3 4 5 6», а не «1 2 3 4»: иначе у того, кто
    работает в силовом диапазоне, кнопок почти не остаётся.

    Цена этого — на 1-3 повторах асимметрия переворачивается: у тройки выходит
    два варианта ниже и три выше, потому что ниже единицы повторов не бывает и
    окну некуда расти вниз. Шесть кнопок выбраны осознанно как важнее ровной
    асимметрии, а случай редкий: последний подход на 1-3 повтора.
    """
    for last in (1, 2, 3):
        window = keyboards.reps_window(last)
        assert len(window) == 6
        assert window[0] == 1
        assert last in window


def test_reps_row_appears_above_the_other_controls():
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=True, last_reps=10)
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    assert rows[0] == [f"live:reps:{n}" for n in (7, 8, 9, 10, 11, 12)]
    # Шесть однозначных кнопок в одной строке — в отличие от «Удалить последний»,
    # которую в половинную колонку не втиснуть.
    assert len(kb.inline_keyboard[0]) == 6


def test_reps_row_stays_first_even_inside_a_superset():
    """Иначе место ряда зависело от размера суперсета: без него первый ряд, с
    двумя упражнениями третий, с тремя четвёртый. Шесть одинаковых цифр труднее
    всего зацепить глазом, и именно им нужно постоянное место — вкладки
    подписаны словами, их находишь чтением."""
    kb = keyboards.logging_keyboard(
        [(1, "Bench"), (2, "Row"), (3, "Curl")], active_id=1, has_sets=True, last_reps=10
    )
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]
    assert rows[0] == [f"live:reps:{n}" for n in (7, 8, 9, 10, 11, 12)]
    assert rows[1:4] == [["live:switch:1"], ["live:switch:2"], ["live:switch:3"]]


def test_no_reps_row_until_there_is_a_weight_to_reuse():
    kb = keyboards.logging_keyboard([(1, "Bench")], active_id=1, has_sets=False)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert not any(c.startswith("live:reps:") for c in cbs)


def test_reps_row_is_captioned_with_the_weight():
    """Шесть цифр без единого слова — загадка. Подпись называет вес, к которому
    они относятся, и этого хватает, чтобы понять остальное."""
    hint = workout._logging_hint(
        None, True, "kg", False, [(25.0, 10)],
        show_instruction=False, reps_row=(25.0, 10),
    )
    assert "Жми повторы на 25кг" in hint


def test_caption_fits_one_line_on_a_phone():
    """Полный вариант («Жми повторы — запишу подход на 200кг») переносился ровно
    перед весом, и вес — единственное, ради чего подпись стоит, — оставался на
    второй строке один. В строку телефона влезает около 32 знаков; держим бюджет
    даже на трёхзначном весе с дробью."""
    hint = workout._logging_hint(
        None, True, "kg", False, [(207.5, 5)],
        show_instruction=False, reps_row=(207.5, 5),
    )
    caption = hint.splitlines()[-1]
    assert len(caption) <= 32, caption


def test_caption_does_not_re_explain_where_the_weight_came_from():
    """«(как в прошлый раз)» повторяло строку «💡 Прошлый раз» дословно, а когда
    весов в прошлый раз было несколько — ещё и спорило с ней: вес взят с
    ПОСЛЕДНЕГО подхода, а не с «прошлого раза» целиком. И «сверху» тоже нет:
    подпись стоит в самом низу сообщения, а цифры под ней — слово гнало глаз
    вверх, в текст, где цифр нет."""
    from_last = workout._logging_hint(
        None, False, "kg", False, [], show_instruction=False, reps_row=(25.0, 10),
    )
    from_today = workout._logging_hint(
        None, True, "kg", False, [(25.0, 10)],
        show_instruction=False, reps_row=(25.0, 10),
    )
    assert "как в прошлый раз" not in from_last
    assert "сверху" not in from_last
    assert from_last.endswith("🔢 Жми повторы на 25кг")
    assert from_today.endswith("🔢 Жми повторы на 25кг")


def test_caption_shows_even_for_seasoned_users():
    """Подсказка ввода гаснет у опытных, а ряд цифр видят все — включая тех, для
    кого он появился впервые после обновления. Подпись не под show_instruction."""
    hint = workout._logging_hint(
        None, True, "kg", False, [(25.0, 10)],
        show_instruction=False, reps_row=(25.0, 10),
    )
    assert "🔢" in hint
    assert "через пробел" not in hint


def test_reps_row_falls_back_to_last_session_before_the_first_set():
    """Первый подход — там кнопки полезнее всего: человек подошёл к снаряду и
    почти всегда начинает с того же веса, что и в прошлый раз."""
    assert workout._reps_row_basis([], [(25.0, 12, None), (25.0, 10, None)]) == (25.0, 12)
    assert workout._reps_row_basis([(30.0, 8)], [(25.0, 10, None)]) == (30.0, 8)
    # Ни сегодня, ни в прошлый раз — брать вес неоткуда, ряда нет.
    assert workout._reps_row_basis([], None) is None


def test_the_first_set_starts_from_the_first_set_of_last_time_not_the_last():
    """Внутри упражнения вес чаще падает, чем растёт: 34.3, 32, 32 — это не
    «рабочий вес 32», это скидка от усталости в конце. Предлагать её как
    стартовый вес значит звать начать слабее, чем человек начал в прошлый раз,
    да ещё и спорить с целью прогрессии, которая считается от верха."""
    last = [(34.3, 4, None), (32.0, 8, None), (32.0, 11, None)]

    assert workout._reps_row_basis([], last) == (34.3, 4)
    # А как только сегодняшний подход записан — правило прежнее, вес с него.
    assert workout._reps_row_basis([(32.0, 5)], last) == (32.0, 5)
