"""Живому тренеру — «Клиент закончил тренировку» (coach_notify.deliver_client_workout).

Владелец: «а мне не должен приходить пуш, если клиент закончил треню? жена
потренила — ничего не пришло». Здесь закреплено:

- оба пути завершения (бот и `POST /v1/workouts/{id}/finish`) шлют тренеру
  ровно одно сообщение: банер, строка в «Уведомлениях», Telegram с кнопкой в
  тренировку клиента; повтор финиша второго не даёт;
- без связки с тренером, с выключенным тумблером «Тренировки клиентов» и у
  тренировки меньше чем из двух подходов — ничего;
- подписи — на языке тренера, название упражнения с рекордом — на языке
  атлета;
- ночью по поясу тренера банера нет, строка в «Уведомлениях» и беззвучный
  Telegram — есть;
- тумблер виден тренеру в настройках бота и в `/v1/settings`;
- пуш тренеру про чужую тренировку не занимает его дневной слот.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiogram
import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import api_v1
import apns
import coach_notify
import config
import engagement
import i18n
import i18n_coverage
import push_ios
import seed_data
from fsm import WorkoutFlow
from handlers import settings as settings_handlers
from handlers import workout as workout_handlers

pytestmark = pytest.mark.asyncio

COACH = 801
ATHLETE = 802
BENCH = "Жим штанги лёжа"  # идентичность каталога
SQUAT = "Присед со штангой"
DAY = dt.datetime(2026, 10, 26, 12, 0)  # 15:00 у тренера (UTC+3)
NIGHT = dt.datetime(2026, 10, 26, 21, 30)  # 00:30 у тренера


class FakeBot:
    """И живой бот хендлера, и короткоживущий REST-слоя."""

    def __init__(self):
        self.sent: list[dict] = []
        self.session = SimpleNamespace(close=AsyncMock())
        self.edit_message_text = AsyncMock()
        self.delete_message = AsyncMock()
        self.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=1))
        self._next = 900

    async def send_message(self, chat_id, text, **kwargs):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next, **kwargs})
        return SimpleNamespace(message_id=self._next)

    def to(self, chat_id: int) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == chat_id]


@pytest.fixture
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setattr(config, "BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(aiogram, "Bot", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def banners(monkeypatch):
    sent: list[dict] = []

    async def send_alert(user_id, token, title, body, **kwargs):
        sent.append({"user_id": user_id, "title": title, "body": body, **kwargs})
        return True

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", send_alert)
    return sent


@pytest.fixture
def clock(monkeypatch):
    now = {"t": DAY}
    monkeypatch.setattr(coach_notify, "_utc_now", lambda: now["t"])
    return now


async def _user(db, telegram_id: int, lang: str):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    await db.set_user_lang(telegram_id, lang)
    await db.update_user(telegram_id, tz_offset=3)
    await db.register_push_token(telegram_id, "ios", f"tok-{telegram_id}")


async def _linked(db, *, coach_lang: str = "ru", athlete_lang: str = "ru", link: bool = True):
    await _user(db, COACH, coach_lang)
    await db.upsert_coach_profile(COACH, "Марина")
    await _user(db, ATHLETE, athlete_lang)
    if link:
        assert await db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=False)


async def _add_sets(db, workout_id: int, exercise_id: int, sets: list[tuple[float, int]]):
    block = await db.create_block(workout_id, "single")
    await db.add_block_exercise(block, exercise_id, 0)
    for i, (weight, reps) in enumerate(sets):
        await db.add_set(block, exercise_id, i + 1, 0, weight, reps)


async def _workout(db, *, sets: int = 3) -> int:
    """Прошлая тренировка (жим 70×5) и текущая, ещё не законченная: жим 80×5
    — рекорд, присед — впервые (не рекорд)."""
    bench = await db.get_or_create_user_exercise_by_name(ATHLETE, BENCH)
    squat = await db.get_or_create_user_exercise_by_name(ATHLETE, SQUAT)
    old = await db.create_workout(ATHLETE, started_at="2026-10-20T10:00:00")
    await _add_sets(db, old, bench, [(70.0, 5)])
    await db.finish_workout(old, finished_at="2026-10-20T11:00:00")
    wid = await db.create_workout(ATHLETE)
    if sets == 1:
        await _add_sets(db, wid, bench, [(80.0, 5)])
    else:
        await _add_sets(db, wid, bench, [(80.0, 5)] * (sets - 1))
        await _add_sets(db, wid, squat, [(100.0, 5)])
    return wid


def _callback(user_id: int, data: str, bot):
    message = MagicMock()
    message.text = "previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2, delete=AsyncMock()))
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None, first_name=None)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def _finish_in_bot(bot, wid: int) -> None:
    state = _state(ATHLETE)
    await state.set_state(WorkoutFlow.idle)
    await state.update_data(live_chat_id=ATHLETE, live_message_id=1, workout_id=wid)
    await workout_handlers._finalize_workout(_callback(ATHLETE, "x", bot), state, note=None)
    await coach_notify.wait_background()


async def _api(db, user_id: int) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {await db.issue_api_token(user_id)}"
    return client


async def _inbox(db, user_id: int) -> list:
    cur = await db.conn().execute("SELECT * FROM inbox WHERE user_id = ? ORDER BY id", (user_id,))
    return await cur.fetchall()


async def _pushes(db, user_id: int) -> list:
    cur = await db.conn().execute("SELECT * FROM pushes WHERE telegram_id = ? ORDER BY id", (user_id,))
    return await cur.fetchall()


def _buttons(kb) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


# ---------- оба пути завершения: ровно одно сообщение ----------


async def test_bot_finish_notifies_the_coach_once_on_every_channel(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    wid = await _workout(db)

    await _finish_in_bot(bot, wid)

    [message] = bot.to(COACH)
    assert message["text"].startswith("🧑‍🏫 <b>КЛИЕНТ ЗАКОНЧИЛ ТРЕНИРОВКУ</b>")
    assert "<b>Маша</b> — 2 упражнения · 1.3 тонны · рекорд: Жим штанги лёжа" in message["text"]
    assert message["disable_notification"] is True
    assert _buttons(message["reply_markup"]) == [
        (i18n.t_in("ru", "coach.btn.open_workout"), f"coach:w:{ATHLETE}:{wid}")
    ]

    [banner] = [b for b in banners if b["user_id"] == COACH]
    assert banner["title"] == "Клиент закончил тренировку"
    assert banner["body"] == "Маша — 2 упражнения · 1.3 тонны · рекорд: Жим штанги лёжа"
    assert banner["category"] == push_ios.COACH_CLIENT_WORKOUT
    assert banner["route"] == {"screen": "coach_clients", "client_id": ATHLETE, "workout_id": wid}
    # Атлету про его же тренировку — ничего.
    assert not [b for b in banners if b["user_id"] == ATHLETE]

    [row] = await _inbox(db, COACH)
    assert row["category"] == push_ios.COACH_CLIENT_WORKOUT and row["title"] == banner["title"]

    [push] = await _pushes(db, COACH)
    assert push["category"] == push_ios.COACH_CLIENT_WORKOUT
    assert push["channel"] == "tg" and push["tg_message_id"] == message["message_id"]
    assert (await db.get_workout(wid))["coach_notified_at"] is not None


async def test_api_finish_notifies_once_and_a_replay_adds_nothing(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    wid = await _workout(db)
    api = await _api(db, ATHLETE)

    resp = await api.post(f"/workouts/{wid}/finish", json={})
    assert resp.status_code == 200, resp.text
    await coach_notify.wait_background()
    # Повтор после оборванного ответа (офлайн-очередь приложения).
    resp = await api.post(f"/workouts/{wid}/finish", json={})
    assert resp.status_code == 200 and resp.json()["replayed"] is True
    await coach_notify.wait_background()

    [message] = bot.to(COACH)
    assert f"coach:w:{ATHLETE}:{wid}" == message["reply_markup"].inline_keyboard[0][0].callback_data
    assert len([b for b in banners if b["user_id"] == COACH]) == 1
    assert len(await _inbox(db, COACH)) == 1


async def test_stale_finish_in_bot_notifies_too(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    wid = await _workout(db)

    callback = _callback(ATHLETE, f"stale:finish:{wid}", bot)
    result = await workout_handlers.router.callback_query.trigger(callback, state=_state(ATHLETE))
    assert result is not UNHANDLED
    await coach_notify.wait_background()

    assert len(bot.to(COACH)) == 1


async def test_repeated_delivery_is_idempotent(fresh_db, bot, banners, clock):
    """Тренировку закончили, вернули в работу и закончили снова — или доставка
    позвана дважды: тренеру одно сообщение."""
    db = fresh_db
    await _linked(db)
    wid = await _workout(db)
    await db.finish_workout(wid)

    assert await coach_notify.deliver_client_workout(ATHLETE, wid, bot=bot) == 1
    assert await coach_notify.deliver_client_workout(ATHLETE, wid, bot=bot) == 0
    await coach_notify.notify_client_workout_finished(ATHLETE, wid, bot=bot)
    await coach_notify.wait_background()

    assert len(bot.to(COACH)) == 1
    assert len([b for b in banners if b["user_id"] == COACH]) == 1


# ---------- когда не шлём ----------


async def test_no_coach_link_sends_nothing(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db, link=False)
    wid = await _workout(db)

    await _finish_in_bot(bot, wid)

    assert bot.to(COACH) == [] and banners == []
    assert await _inbox(db, COACH) == []
    assert (await db.get_workout(wid))["coach_notified_at"] is None


async def test_toggle_off_sends_nothing(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    await db.update_user(COACH, client_workout_pushes_enabled=0)
    wid = await _workout(db)

    await _finish_in_bot(bot, wid)

    assert bot.to(COACH) == [] and banners == []
    assert await _inbox(db, COACH) == []


async def test_fewer_than_two_sets_sends_nothing(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    wid = await _workout(db, sets=1)

    await _finish_in_bot(bot, wid)

    assert bot.to(COACH) == [] and banners == []
    assert await _inbox(db, COACH) == []
    assert (await db.get_workout(wid))["coach_notified_at"] is None


# ---------- язык: хром тренера, данные атлета ----------


async def test_english_coach_russian_athlete(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db, coach_lang="en", athlete_lang="ru")
    wid = await _workout(db)
    await db.finish_workout(wid)

    await coach_notify.deliver_client_workout(ATHLETE, wid, bot=bot)

    bench_ru = seed_data.localized_exercise_name(BENCH, "ru")
    [banner] = banners
    assert banner["title"] == "A client finished a workout"
    assert banner["body"] == f"Маша — 2 exercises · 1.3 tons · record: {bench_ru}"
    [message] = bot.to(COACH)
    assert "A CLIENT FINISHED A WORKOUT" in message["text"]
    assert message["reply_markup"].inline_keyboard[0][0].text == i18n.t_in("en", "coach.btn.open_workout")
    for shown in (banner["body"], message["text"]):
        assert not i18n_coverage.has_cyrillic(shown.replace("Маша", "").replace(bench_ru, "")), shown


async def test_russian_coach_english_athlete_sees_english_exercise(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db, coach_lang="ru", athlete_lang="en")
    wid = await _workout(db)
    await db.finish_workout(wid)

    await coach_notify.deliver_client_workout(ATHLETE, wid, bot=bot)

    bench_en = seed_data.localized_exercise_name(BENCH, "en")
    [banner] = banners
    assert banner["title"] == "Клиент закончил тренировку"
    assert banner["body"].endswith(f"рекорд: {bench_en}")


async def test_summary_drops_missing_facts():
    """Тоннажа нет — его не пишем; рекордов несколько — числом."""
    facts = coach_notify.ClientWorkoutFacts(exercises=1, sets=3, tonnage=0.0, unit="kg", records=[])
    assert coach_notify.client_workout_summary("ru", facts) == "1 упражнение"
    facts = coach_notify.ClientWorkoutFacts(
        exercises=5, sets=15, tonnage=3200.0, unit="kg", records=["А", "Б", "В"]
    )
    assert coach_notify.client_workout_summary("ru", facts) == "5 упражнений · 3.2 тонны · 3 рекорда"
    assert coach_notify.client_workout_summary("en", facts) == "5 exercises · 3.2 tons · 3 records"


# ---------- тихие часы ----------


async def test_night_skips_the_banner_but_keeps_inbox_and_silent_telegram(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    clock["t"] = NIGHT
    wid = await _workout(db)

    await _finish_in_bot(bot, wid)

    assert banners == []
    [row] = await _inbox(db, COACH)
    assert row["title"] == "Клиент закончил тренировку"
    [message] = bot.to(COACH)
    assert message["disable_notification"] is True


async def test_quiet_time_uses_the_coach_timezone(fresh_db, clock):
    db = fresh_db
    await _linked(db)
    coach = await db.get_user(COACH)
    clock["t"] = dt.datetime(2026, 10, 26, 20, 0)  # 23:00 у UTC+3
    assert coach_notify.is_coach_quiet_time(coach)
    await db.update_user(COACH, tz_offset=-5)  # 15:00 в Нью-Йорке
    assert not coach_notify.is_coach_quiet_time(await db.get_user(COACH))


# ---------- тумблер ----------


async def test_bot_settings_toggle_only_for_coaches(fresh_db, clock):
    db = fresh_db
    await _linked(db)

    def labels(callback) -> list[tuple[str, str]]:
        # Экран встаёт правкой на месте или новым сообщением (ui.safe_edit).
        call = callback.message.edit_text.await_args or callback.message.answer.await_args
        return _buttons(call.kwargs["reply_markup"])

    athlete_cb = _callback(ATHLETE, "settings:menu", FakeBot())
    await settings_handlers.router.callback_query.trigger(athlete_cb, state=_state(ATHLETE))
    assert not [cb for _t, cb in labels(athlete_cb) if cb == "settings:client_workouts"]

    coach_cb = _callback(COACH, "settings:menu", FakeBot())
    await settings_handlers.router.callback_query.trigger(coach_cb, state=_state(COACH))
    assert (i18n.t_in("ru", "btn.client_workouts_on"), "settings:client_workouts") in labels(coach_cb)

    toggle = _callback(COACH, "settings:client_workouts", FakeBot())
    with i18n.use_lang("ru"):
        await settings_handlers.router.callback_query.trigger(toggle, state=_state(COACH))
    assert (await db.get_user(COACH))["client_workout_pushes_enabled"] == 0
    assert (i18n.t_in("ru", "btn.client_workouts_off"), "settings:client_workouts") in labels(toggle)


async def test_v1_settings_expose_the_toggle(fresh_db):
    db = fresh_db
    await _linked(db)
    api = await _api(db, COACH)

    assert (await api.get("/settings")).json()["client_workout_pushes_enabled"] is True
    resp = await api.patch("/settings", json={"client_workout_pushes_enabled": False})
    assert resp.status_code == 200 and resp.json()["client_workout_pushes_enabled"] is False
    assert (await db.get_user(COACH))["client_workout_pushes_enabled"] == 0


# ---------- дневной слот тренера ----------


async def test_coach_push_does_not_take_the_coach_daily_slot(fresh_db, bot, banners, clock):
    db = fresh_db
    await _linked(db)
    wid = await _workout(db)
    await db.finish_workout(wid)
    await coach_notify.deliver_client_workout(ATHLETE, wid, bot=bot)

    today = coach_notify.coach_local_now(await db.get_user(COACH)).date().isoformat()
    assert await db.has_push_today(COACH, today)
    assert not await db.has_push_today(
        COACH, today, exclude_categories=engagement.DAILY_SLOT_EXCLUDED_CATEGORIES
    )
