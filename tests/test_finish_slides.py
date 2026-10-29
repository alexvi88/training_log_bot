"""Слайды итогов тренировки (finish_slides.py): какие карточки попадают в
альбом, размер картинки, язык и глифы того, что уезжает в PNG, и отправка
альбома после «Завершить» (handlers.workout._finalize_workout)."""

import asyncio
import datetime as dt
import io
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InputMediaPhoto
from PIL import Image

import config
import finish_slides as fs
import formatting as f
import i18n
from fsm import WorkoutFlow
from handlers import workout

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
STARTED = dt.datetime(2026, 10, 29, 18, 0)
USER = {"unit": "kg", "show_extra_stats": 1, "gender": None, "tz_offset": 0}


def setup_function(_):
    i18n.reload()


def _block(ex_id, name, sets, **kwargs):
    return f.ExerciseBlockView("Chest", name, sets, exercise_id=ex_id, **kwargs)


def _deck(blocks, lang="en", user=USER, duration=3600):
    with i18n.use_lang(lang):
        return fs.assemble(blocks, user, started_at=STARTED, duration_seconds=duration)


# ---------- какие карточки есть ----------


def test_no_tonnage_no_barbell_but_summary_stays():
    deck = _deck([_block(1, "Pull-ups", [(0, 12), (0, 10)])])
    assert deck.barbell is None
    assert [type(s) for s in deck.slides] == [fs.SummarySlide]
    # Без тоннажа цифра «Тренировки» — подходы, а не «0кг».
    assert deck.summary.value == "2"


def test_records_capped_at_three_biggest_gain_first():
    blocks = [
        _block(1, "A", [(100, 5)], record_e1rm_delta=1.0),   # ~0.9%
        _block(2, "B", [(100, 5)], record_e1rm_delta=5.0),   # ~4.3%
        _block(3, "C", [(0, 12)], record_reps=12, record_reps_prev=9),  # 25%
        _block(4, "D", [(100, 5)], record_e1rm_delta=3.0),
        _block(5, "E", [(100, 5)], record_e1rm_delta=2.0),
    ]
    deck = _deck(blocks)
    kinds = [type(s) for s in deck.slides]
    assert kinds == [fs.BarbellSlide, fs.RecordSlide, fs.RecordSlide, fs.RecordSlide, fs.SummarySlide]
    assert [r.exercise_name for r in deck.record_cards] == ["C", "B", "D"]
    reps = deck.record_cards[0]
    assert (reps.value, reps.prefix, reps.facts[0].value) == ("12", None, "+3")
    bench = deck.record_cards[1]
    assert bench.prefix == "e1RM" and bench.facts[0].value == "+5kg" and bench.facts[1].value == "100×5"
    # Рекорды без своей карточки всё равно отмечены в «Тренировке».
    assert [row.badge for row in deck.summary.rows] == ["+1kg", "+5kg", "★", "+3kg", "+2kg"]


def test_e1rm_record_silent_without_extra_stats():
    blocks = [
        _block(1, "Bench", [(100, 5)], record_e1rm_delta=5.0),
        _block(2, "Pull-ups", [(0, 12)], record_reps=12, record_reps_prev=10),
    ]
    deck = _deck(blocks, user={**USER, "show_extra_stats": 0})
    assert [r.exercise_name for r in deck.record_cards] == ["Pull-ups"]
    assert deck.summary.rows[0].badge is None


def test_empty_workout_has_no_slides():
    deck = _deck([_block(1, "Bench", [])])
    assert deck.slides == []


def test_summary_rows_capped_with_more_line():
    blocks = [_block(i, f"Ex {i}", [(50, 10)]) for i in range(1, 14)]
    deck = _deck(blocks)
    assert len(deck.summary.rows) == fs.SUMMARY_MAX_EXERCISES
    assert deck.summary.more == "3 more"


def test_summary_and_barbell_numbers_match_finish_text():
    blocks = [_block(1, "Bench", [(100, 5), (100, 5), (100, 5)]), _block(2, "Squat", [(140, 5)] * 4)]
    deck = _deck(blocks, lang="ru")
    with i18n.use_lang("ru"):
        expected = f.format_tonnage(100 * 15 + 140 * 20, "kg")
    assert f"{deck.barbell.value} {deck.barbell.unit}" == expected
    assert [fact.value for fact in deck.barbell.facts] == ["2", "7", "1 ч"]
    assert deck.summary.rows[0].sets == "100×5 ×3"


def test_lb_account_gets_lb_plates():
    deck = _deck([_block(1, "Bench", [(225, 5)] * 5)], user={**USER, "unit": "lb"})
    assert deck.barbell.plate_mark == "LB" and deck.barbell.plate_weight == 100
    assert deck.barbell.unit == "lb"
    assert fs.plates_per_side(deck.barbell) == (28, 56)  # 5625lb / 200lb на пару


def test_female_athlete_caption():
    deck = _deck([_block(1, "Жим", [(40, 10)])], lang="ru", user={**USER, "gender": "female"})
    assert deck.barbell.lifted == "Подняла в сумме"


# ---------- картинка ----------


def _sample_blocks(lang):
    name = "Жим штанги лёжа" if lang == "ru" else "Bench press"
    return [
        _block(1, name, [(100, 5)] * 3, record_e1rm_delta=4.0),
        _block(2, "Pull-ups" if lang != "ru" else "Подтягивания", [(0, 12)], record_reps=12, record_reps_prev=10),
    ]


def test_every_slide_is_1080x1920_png():
    deck = _deck(_sample_blocks("en"))
    pngs = fs.render_deck(deck)
    assert len(pngs) == len(deck.slides) == 4
    for png in pngs:
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert Image.open(io.BytesIO(png)).size == (1080, 1920)


@pytest.mark.parametrize("lang", i18n.SUPPORTED)
def test_slide_text_has_glyphs_and_right_language(lang):
    deck = _deck(_sample_blocks(lang), lang=lang)
    for slide in deck.slides:
        texts = fs.slide_texts(slide, deck.signature)
        joined = " ".join(texts + [fs._upper(t, lang) for t in texts])
        assert not fs.missing_glyphs(joined, bold=True), (lang, fs.missing_glyphs(joined))
        assert not fs.missing_glyphs(joined, bold=False), (lang, fs.missing_glyphs(joined, bold=False))
        assert "&" not in joined and "<" not in joined, joined
        if lang != "ru":
            assert not CYRILLIC.search(joined), (lang, joined)
    # И сам рендер на этом языке проходит.
    assert len(fs.render_deck(deck)) == len(deck.slides)


def test_turkish_caps_keep_dotted_i():
    assert fs._upper("Antrenmanım için", "tr") == "ANTRENMANIM İÇİN"


# ---------- отправка после «Завершить» ----------


def _make_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _make_callback(user_id, bot):
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.bot = bot
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id, **data):
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=user_id, live_message_id=1, **data)
    return state


async def _log(db, workout_id, exercise_id, weight, reps):
    block_id = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block_id, exercise_id, 0)
    await db.add_set(block_id, exercise_id, 1, 0, weight, reps)


async def _drain():
    while workout._background_tasks:
        await asyncio.gather(*list(workout._background_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_finish_sends_album_with_record(fresh_db, user_id, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(config, "FINISH_SLIDES_ENABLED", True)
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group)
    w1 = await db.create_workout(user_id, started_at="2026-10-01T12:00:00")
    await _log(db, w1, bench, 100, 5)
    await db.finish_workout(w1, finished_at="2026-10-01T13:00:00")
    w2 = await db.create_workout(user_id)
    await _log(db, w2, bench, 110, 5)

    bot = _make_bot()
    await workout._finalize_workout(_make_callback(user_id, bot), await _make_state(user_id, workout_id=w2), None)
    await _drain()

    bot.send_media_group.assert_awaited_once()
    kwargs = bot.send_media_group.await_args.kwargs
    assert kwargs["chat_id"] == user_id
    media = kwargs["media"]
    # Штанга, рекорд жима, «Тренировка».
    assert len(media) == 3 and all(isinstance(m, InputMediaPhoto) for m in media)
    first = media[0].media.data
    assert Image.open(io.BytesIO(first)).size == (1080, 1920)


@pytest.mark.asyncio
async def test_finish_single_slide_goes_as_photo(fresh_db, user_id, monkeypatch):
    """Одни подтягивания без веса тела: тоннажа нет, рекорда нет — один слайд,
    и он уходит обычным фото (альбом из одного Telegram не принимает)."""
    db = fresh_db
    monkeypatch.setattr(config, "FINISH_SLIDES_ENABLED", True)
    group = await db.create_muscle_group(user_id, "Спина")
    pullups = await db.create_exercise(user_id, "Pull-ups", group)
    w = await db.create_workout(user_id)
    await _log(db, w, pullups, 0, 10)

    bot = _make_bot()
    await workout._finalize_workout(_make_callback(user_id, bot), await _make_state(user_id, workout_id=w), None)
    await _drain()

    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_failure_never_breaks_finish(fresh_db, user_id, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(config, "FINISH_SLIDES_ENABLED", True)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(fs, "build", boom)
    group = await db.create_muscle_group(user_id, "Грудь")
    bench = await db.create_exercise(user_id, "Bench press", group)
    w = await db.create_workout(user_id)
    await _log(db, w, bench, 100, 5)

    bot = _make_bot()
    await workout._finalize_workout(_make_callback(user_id, bot), await _make_state(user_id, workout_id=w), None)
    await _drain()

    assert (await db.get_workout(w))["status"] == "finished"
    bot.edit_message_text.assert_awaited_once()
    bot.send_media_group.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_skips_workout_without_sets(fresh_db, user_id):
    db = fresh_db
    w = await db.create_workout(user_id)
    user = await db.get_user(user_id)
    assert await fs.build(w, user) == []
