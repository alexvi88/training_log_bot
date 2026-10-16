"""Главный экран «🏆 Достижения» и два экрана за его кнопками.

Главный — звание с полоской, одна строка из трёх чисел, рекорды только в
базовых движениях (по идентичности каталога, а не по имени на экране), счёт
значков, до трёх ближайших и до трёх последних. Закрытого списка на нём нет:
он — на «Все ачивки», полный список рекордов — на «Все рекорды».

Экраны прогоняются через настоящий роутер (handlers.history.router), как
кнопки в tests/test_coach_bot.py, на обоих языках.
"""

from __future__ import annotations

import datetime as dt
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import achievement_sync
import achievements
import analytics
import formatting
import hall_of_fame_data
import i18n
import seed_data
from handlers import history

pytestmark = pytest.mark.asyncio

ATHLETE = 111


def _callback(user_id: int, data: str):
    message = MagicMock()
    message.text = "previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2, delete=AsyncMock()))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.bot = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _tap(user_id: int, data: str):
    """Нажать кнопку через настоящий роутер — ловит и пересечения префиксов."""
    callback = _callback(user_id, data)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    result = await history.router.callback_query.trigger(callback, state=state)
    assert result is not UNHANDLED, f"кнопку {data} не забрал никто"
    return callback


def _screen(callback) -> str:
    return callback.message.answer.await_args.args[0]


def _buttons(callback) -> list[tuple[str, str]]:
    kb = callback.message.answer.await_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


async def _template_id(db, name: str) -> int:
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE is_template = 1 AND user_id IS NULL AND name = ?", (name,)
    )
    return (await cur.fetchone())["id"]


async def _workout(db, user_id: int, when: dt.date, lifts: list[tuple[int, list[tuple[float, int]]]]):
    wid = await db.create_workout(user_id, started_at=f"{when.isoformat()}T10:00:00")
    for ex_id, sets in lifts:
        block_id = await db.create_block(wid, "single")
        await db.add_block_exercise(block_id, ex_id, 0)
        for weight, reps in sets:
            await db.append_set(block_id, ex_id, 0, weight, reps)
    await db.finish_workout(wid, finished_at=f"{when.isoformat()}T11:00:00")


async def seed_lifter(db, user_id: int, lang: str) -> dict[str, int]:
    """Атлет с базовыми движениями из каталога, тренажёром и подтягиваниями —
    двенадцать тренировок раз в три дня. Становую он переименовал: идентичность
    остаётся каталожной, и экран обязан узнать её всё равно."""
    await db.set_user_lang(user_id, lang)
    ids = {}
    for key, name in (
        ("squat", "Присед со штангой"), ("bench", "Жим штанги лёжа"),
        ("deadlift", "Становая тяга"), ("machine", "Жим в тренажёре"),
        ("pullup", "Подтягивания"),
    ):
        ids[key] = await db.fork_exercise_from_template(user_id, await _template_id(db, name))
    await db.conn().execute(
        "UPDATE exercises SET display_name = 'My pull from the floor', name = 'My pull from the floor' WHERE id = ?",
        (ids["deadlift"],),
    )
    await db.conn().commit()
    today = dt.date.today()
    for i in range(12):
        await _workout(db, user_id, today - dt.timedelta(days=3 * (11 - i)), [
            (ids["squat"], [(100.0 + i * 2.5, 5)] * 3),
            (ids["bench"], [(80.0 + i, 5)] * 3),
            (ids["deadlift"], [(140.0 + i * 5, 3)] * 2),
            (ids["machine"], [(60.0, 12)] * 3),
            (ids["pullup"], [(0.0, 8 + i // 3)]),
        ])
    await achievement_sync.resync(user_id)
    return ids


async def seed_machine_only(db, user_id: int, lang: str) -> None:
    """Ни одного базового движения: тренажёры и изоляция."""
    await db.set_user_lang(user_id, lang)
    machine = await db.fork_exercise_from_template(user_id, await _template_id(db, "Жим в тренажёре"))
    legs = await db.fork_exercise_from_template(user_id, await _template_id(db, "Разгибание ног в тренажёре"))
    group = await db.create_muscle_group(user_id, "Свои")
    curl = await db.create_exercise(user_id, "Curl X", group)
    rare = await db.create_exercise(user_id, "Rare one", group)
    today = dt.date.today()
    for i in range(5):
        lifts = [(machine, [(60.0, 12)]), (legs, [(40.0, 15)])]
        if i < 3:
            lifts.append((curl, [(20.0, 10)]))
        if i == 0:
            lifts.append((rare, [(200.0, 1)]))  # самый тяжёлый, но разовый
        await _workout(db, user_id, today - dt.timedelta(days=i * 2), lifts)
    await achievement_sync.resync(user_id)


# ---------- главный экран ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_main_screen_shows_main_lifts_only_and_no_locked_list(fresh_db, user_id, lang):
    await seed_lifter(fresh_db, user_id, lang)
    with i18n.use_lang(lang):
        cb = await _tap(user_id, "menu:achievements")
        text = _screen(cb)
        squat = seed_data.localized_exercise_name("Присед со штангой", lang)

    # Звание с полоской и тремя числами одной строкой.
    assert "▰" in text or "▱" in text
    stats = next(line for line in text.splitlines() if line.startswith("📅"))
    assert "🏋️" in stats and "🔥" in stats
    # Рекорды — только базовые, переименованная становая узнана по идентичности.
    assert squat in text
    assert "My pull from the floor" in text
    assert text.count("e1RM") == 3
    assert seed_data.localized_exercise_name("Жим в тренажёре", lang) not in text
    # Закрытый список — не здесь.
    assert "🔒" not in text
    with i18n.use_lang(lang):
        assert i18n.t("achievements.locked_header", n=1).split("—")[0] not in text
        assert i18n.t("achievements.home.header", got=len(await fresh_db.list_achievement_codes(user_id)),
                      total=len(achievements.CATALOG)) in text
        nearest_header = i18n.t("achievements.nearest_header")
        recent_header = i18n.t("achievements.recent_header")
    assert nearest_header in text and recent_header in text
    nearest = text.split(nearest_header)[1].split(recent_header)[0].strip().splitlines()
    assert 1 <= len(nearest) <= 3
    recent = text.split(recent_header)[1].strip().splitlines()
    assert 1 <= len(recent) <= 3
    # Дата получения — у последних значков.
    assert all(" · " in line for line in recent)
    # Самой длинной тренировки и старого «показано N из M» больше нет.
    assert "⏱" not in text
    if lang == "en":
        assert not any("а" <= ch <= "я" for ch in text.lower())

    data = [d for _, d in _buttons(cb)]
    assert data == ["ach:records:0", "ach:all", "rank:ladder", "invite:show", "hist:menu"]


async def test_main_lifts_follow_identity_not_the_shown_name(fresh_db, user_id):
    """Своё упражнение по имени «Присед со штангой» без связи с каталогом
    (копия «⧉», у неё original_name NULL) — не базовое движение."""
    group = await fresh_db.create_muscle_group(user_id, "Ноги")
    fake = await fresh_db.create_exercise(user_id, "Присед со штангой (копия)", group)
    await fresh_db.conn().execute("UPDATE exercises SET original_name = NULL WHERE id = ?", (fake,))
    await fresh_db.conn().commit()
    await _workout(fresh_db, user_id, dt.date.today(), [(fake, [(100.0, 5)])])
    hof = await hall_of_fame_data.collect(user_id)
    headline = await hall_of_fame_data.headline_lifts(user_id, hof)
    assert headline.main is False


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_without_main_lifts_shows_top3_most_trained_without_e1rm(fresh_db, user_id, lang):
    await seed_machine_only(fresh_db, user_id, lang)
    with i18n.use_lang(lang):
        text = _screen(await _tap(user_id, "menu:achievements"))
        header = i18n.t("achievements.home.records_top")
    assert header in text
    block = text.split(header)[1].split("\n\n")[0].strip().splitlines()
    assert len(block) == 3
    assert "Curl X" in text
    assert "Rare one" not in text, "разовое упражнение не из самых частых"
    assert "e1RM" not in text


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_new_athlete_main_screen(fresh_db, user_id, lang):
    await fresh_db.set_user_lang(user_id, lang)
    with i18n.use_lang(lang):
        cb = await _tap(user_id, "menu:achievements")
        text = _screen(cb)
        assert i18n.t("hall.empty") in text
        assert i18n.t("achievements.home.header", got=0, total=len(achievements.CATALOG)) in text
        first = achievements.CATALOG[0]
        # Следующий шаг, а не «пока пусто»: цель первого значка.
        assert first.goal in text
        assert i18n.t("rank.line", emoji=analytics.RANKS[0].emoji, name=analytics.RANKS[0].name) in text
    assert "▱▱▱▱▱▱▱▱▱▱" in text
    assert "e1RM" not in text
    assert text.count("🔒") == 1


async def test_rank_bar_never_lies_at_the_edges():
    assert formatting.rank_progress_bar(0.0) == "▱" * 10
    assert formatting.rank_progress_bar(0.01).startswith("▰▱")
    assert formatting.rank_progress_bar(0.999) == "▰" * 9 + "▱"
    assert formatting.rank_progress_bar(1.0) == "▰" * 10


async def test_rank_progress_follows_the_axis_the_gap_names():
    rank = analytics.RANKS[3]  # 40 тренировок → 80 у следующего
    gap = analytics.RankGap("workouts", 20)
    assert analytics.rank_progress(rank, gap, 60, 1e9, 10) == pytest.approx(0.5)
    assert analytics.rank_progress(analytics.RANKS[-1], None, 1000, 1e9, 10) is None


# ---------- «Все рекорды» ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_all_records_lists_every_exercise(fresh_db, user_id, lang):
    await seed_lifter(fresh_db, user_id, lang)
    with i18n.use_lang(lang):
        cb = await _tap(user_id, "ach:records:0")
        text = _screen(cb)
        assert text.startswith(i18n.t("hall.records_header"))
    machine = seed_data.localized_exercise_name("Жим в тренажёре", lang)
    assert machine in text and "My pull from the floor" in text
    assert text.count("• ") == 5
    assert [d for _, d in _buttons(cb)] == ["menu:achievements"]


async def test_all_records_paginates(fresh_db, user_id):
    group = await fresh_db.create_muscle_group(user_id, "Свои")
    lifts = []
    for i in range(formatting.RECORDS_PAGE_SIZE + 3):
        lifts.append((await fresh_db.create_exercise(user_id, f"Ex {i:02d}", group), [(10.0 + i, 5)]))
    await _workout(fresh_db, user_id, dt.date.today(), lifts)

    first = await _tap(user_id, "ach:records:0:prog")
    assert _screen(first).count("• ") == formatting.RECORDS_PAGE_SIZE
    assert "Страница 1 из 2" in _screen(first)
    assert [d for _, d in _buttons(first)] == ["ach:records:1:prog", "menu:achievements:prog"]
    second = await _tap(user_id, "ach:records:1:prog")
    assert _screen(second).count("• ") == 3
    assert [d for _, d in _buttons(second)] == ["ach:records:0:prog", "menu:achievements:prog"]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_all_records_empty(fresh_db, user_id, lang):
    await fresh_db.set_user_lang(user_id, lang)
    with i18n.use_lang(lang):
        text = _screen(await _tap(user_id, "ach:records:0"))
        assert i18n.t("hall.empty") in text


# ---------- «Все ачивки» ----------


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_all_achievements_lists_earned_and_locked(fresh_db, user_id, lang):
    await seed_lifter(fresh_db, user_id, lang)
    earned = await fresh_db.list_achievement_codes(user_id)
    with i18n.use_lang(lang):
        cb = await _tap(user_id, "ach:all")
        text = _screen(cb)
        assert i18n.t("achievements.screen_header", got=len(earned), total=len(achievements.CATALOG)) in text
        for a in achievements.CATALOG:
            assert escape(a.title) in text
    assert text.count("🔒") == len(achievements.CATALOG) - len(earned)
    assert [d for _, d in _buttons(cb)] == ["menu:achievements"]


async def test_all_achievements_new_athlete(fresh_db, user_id):
    text = _screen(await _tap(user_id, "ach:all"))
    assert "0 из" in text
    assert text.count("🔒") == len(achievements.CATALOG)
