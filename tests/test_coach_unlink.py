"""Разрыв связки тренер ↔ атлет больше не проходит молча (coach_notify.deliver_unlinked).

- атлет закрыл доступ («Закрыть доступ» в боте и `DELETE /v1/coach-links/{id}`)
  → тренеру «Клиент закрыл доступ» в Telegram, банером и строкой в
  «Уведомлениях» — на языке тренера; в «Моих клиентах» неделю стоит серая
  строка «Маша больше не делится дневником · 14 окт» (`recently_unlinked`);
- тренер убрал клиента («Убрать из клиентов» в боте и `DELETE
  /v1/coach/clients/{id}`) → атлету «Тренер больше не ведёт тебя» на языке
  атлета; про программы — только если программа от тренера у атлета правда
  осталась. Серой строки тренеру нет: он убрал сам и знает.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
import i18n
import i18n_coverage
from handlers import coach

pytestmark = pytest.mark.asyncio

COACH = 701
ATHLETE = 702


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    return bot


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


async def _tap(user_id: int, data: str, bot=None):
    callback = _callback(user_id, data, bot or _bot())
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    result = await coach.router.callback_query.trigger(callback, state=state)
    assert result is not UNHANDLED, f"кнопку {data} не забрал никто"
    return callback


def _screen(callback) -> str:
    return callback.message.answer.await_args.args[0]


async def _user(db, telegram_id: int, lang: str):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    await db.set_user_lang(telegram_id, lang)
    # С приложением: пуш ложится и в «Уведомления».
    await db.register_push_token(telegram_id, "ios", f"tok-{telegram_id}")


async def _linked(db, *, coach_lang: str = "ru", athlete_lang: str = "ru"):
    await _user(db, COACH, coach_lang)
    await db.upsert_coach_profile(COACH, "Марина")
    await db.replace_coach_invite(COACH, "K7MP2QXA")
    await _user(db, ATHLETE, athlete_lang)
    assert await db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=False)


@pytest.fixture
def banners(monkeypatch):
    sent: list[dict] = []

    async def send_alert(user_id, token, title, body, **kwargs):
        sent.append({"user_id": user_id, "title": title, "body": body, **kwargs})
        return True

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", send_alert)
    return sent


async def _api(db, user_id: int) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {await db.issue_api_token(user_id)}"
    return client


async def _assign_program(db) -> int:
    import coach_service

    gid = await db.create_muscle_group(COACH, "Спина")
    ex = await db.create_exercise(COACH, "Тяга штанги", gid)
    pid = await db.create_program(COACH, "Сплит")
    day = await db.create_routine(COACH, "День А", program_id=pid)
    await db.add_routine_exercise(day, ex, 0, "3×8")
    new_id, _name = await coach_service.assign_program(COACH, ATHLETE, pid)
    return new_id


def _age_unlink(db, days: float):
    when = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=days))
    return db.conn().execute(
        "UPDATE coach_unlinks SET unlinked_at = ? WHERE coach_id = ? AND client_id = ?",
        (when.isoformat(timespec="seconds"), COACH, ATHLETE),
    )


# ---------- атлет закрыл доступ → тренеру ----------


@pytest.mark.parametrize("coach_lang, title, tg_head", [
    ("ru", "Клиент закрыл доступ", "КЛИЕНТ ЗАКРЫЛ ДОСТУП"),
    ("en", "A client closed access", "A CLIENT CLOSED ACCESS"),
])
async def test_athlete_revoke_in_bot_reaches_the_coach_in_coach_language(
    fresh_db, banners, coach_lang, title, tg_head
):
    # Язык атлета другой — тренеру это не важно.
    await _linked(fresh_db, coach_lang=coach_lang, athlete_lang="en" if coach_lang == "ru" else "ru")
    bot = _bot()
    with i18n.use_lang("en" if coach_lang == "ru" else "ru"):
        await _tap(ATHLETE, f"coach:rvy:{COACH}", bot=bot)
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None

    chat_id, body = bot.send_message.await_args.args[:2]
    assert chat_id == COACH and tg_head in body and "Маша" in body
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        (i18n.t_in(coach_lang, "coach.btn.my_clients_short"), "coach:clients:0")
    ]
    [banner] = banners
    assert banner["user_id"] == COACH and banner["title"] == title and "Маша" in banner["body"]
    assert banner["route"] == {"screen": "coach_clients"} and banner["category"] == "coach_client_left"
    if coach_lang == "en":
        assert not i18n_coverage.has_cyrillic(body.replace("Маша", ""))
        assert not i18n_coverage.has_cyrillic(banner["body"].replace("Маша", ""))

    [row] = await fresh_db.list_inbox(COACH, coach_lang)
    assert row["category"] == "coach_client_left" and row["title"] == title
    # Атлету — ничего: он сам закрыл, экран ему уже ответил.
    assert await fresh_db.list_inbox(ATHLETE, "ru") == [] and await fresh_db.list_inbox(ATHLETE, "en") == []


async def test_athlete_revoke_over_rest_notifies_coach_and_leaves_grey_row(fresh_db, banners):
    await _linked(fresh_db, coach_lang="en")
    athlete_api = await _api(fresh_db, ATHLETE)
    coach_api = await _api(fresh_db, COACH)
    resp = await athlete_api.delete(f"/coach-links/{COACH}")
    assert resp.status_code == 200, resp.text
    [banner] = banners
    assert banner["user_id"] == COACH and banner["title"] == "A client closed access"
    [row] = await fresh_db.list_inbox(COACH, "en")
    assert row["category"] == "coach_client_left"

    data = (await coach_api.get("/coach/clients")).json()
    assert data["clients"] == []
    [gone] = data["recently_unlinked"]
    assert gone["client_id"] == ATHLETE and gone["name"] == "Маша" and gone["unlinked_at"]
    # Клиента открыть нельзя — серая строка не ссылка.
    assert (await coach_api.get(f"/coach/clients/{ATHLETE}")).status_code == 404
    await athlete_api.aclose()
    await coach_api.aclose()


async def test_grey_row_in_bot_clients_list_and_gone_after_a_week(fresh_db, banners):
    await _linked(fresh_db)
    await _tap(ATHLETE, f"coach:rvy:{COACH}")
    shown = await _tap(COACH, "coach:clients:0")
    assert "Маша больше не делится дневником · " in _screen(shown)
    # Строка — не кнопка.
    kb = shown.message.answer.await_args.kwargs["reply_markup"]
    assert all("Маша" not in b.text for row in kb.inline_keyboard for b in row)

    coach_api = await _api(fresh_db, COACH)
    await _age_unlink(fresh_db, config.COACH_UNLINK_SHOWN_DAYS - 0.5)
    assert len((await coach_api.get("/coach/clients")).json()["recently_unlinked"]) == 1
    await _age_unlink(fresh_db, config.COACH_UNLINK_SHOWN_DAYS + 0.5)
    assert (await coach_api.get("/coach/clients")).json()["recently_unlinked"] == []
    assert "Маша" not in _screen(await _tap(COACH, "coach:clients:0"))
    # Чистка стирает и саму строку с именем бывшего клиента.
    assert await fresh_db.prune_old_coach_unlinks(config.COACH_UNLINK_SHOWN_DAYS) == 1
    await coach_api.aclose()


async def test_english_grey_row_has_no_cyrillic_beyond_the_name(fresh_db, banners):
    await _linked(fresh_db, coach_lang="en")
    await _tap(ATHLETE, f"coach:rvy:{COACH}")
    with i18n.use_lang("en"):
        screen = _screen(await _tap(COACH, "coach:clients:0"))
    assert "Маша stopped sharing their diary · " in screen
    assert not i18n_coverage.has_cyrillic(screen.replace("Маша", ""))


async def test_relink_removes_the_grey_row(fresh_db, banners):
    await _linked(fresh_db)
    athlete_api = await _api(fresh_db, ATHLETE)
    coach_api = await _api(fresh_db, COACH)
    assert (await athlete_api.delete(f"/coach-links/{COACH}")).status_code == 200
    assert len((await coach_api.get("/coach/clients")).json()["recently_unlinked"]) == 1
    resp = await athlete_api.post("/coach/invites/K7MP2QXA/accept", json={"name": "Маша"})
    assert resp.status_code == 201, resp.text
    data = (await coach_api.get("/coach/clients")).json()
    assert [c["client_id"] for c in data["clients"]] == [ATHLETE]
    assert data["recently_unlinked"] == []
    await athlete_api.aclose()
    await coach_api.aclose()


# ---------- тренер убрал клиента → атлету ----------


@pytest.mark.parametrize("athlete_lang, title, tg_head", [
    ("ru", "Тренер больше не ведёт тебя", "ТРЕНЕР БОЛЬШЕ НЕ ВЕДЁТ ТЕБЯ"),
    ("en", "Coaching ended", "COACHING ENDED"),
])
async def test_coach_remove_in_bot_reaches_the_athlete_in_athlete_language(
    fresh_db, banners, athlete_lang, title, tg_head
):
    await _linked(fresh_db, coach_lang="en" if athlete_lang == "ru" else "ru", athlete_lang=athlete_lang)
    program_id = await _assign_program(fresh_db)
    banners.clear()
    bot = _bot()
    with i18n.use_lang("en" if athlete_lang == "ru" else "ru"):
        await _tap(COACH, f"coach:rmy:{ATHLETE}", bot=bot)
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None

    chat_id, body = bot.send_message.await_args.args[:2]
    assert chat_id == ATHLETE and tg_head in body and "Марина" in body
    # Программа от тренера правда осталась у атлета — и текст про неё есть.
    assert await fresh_db.list_coach_assignments(COACH, ATHLETE)
    assert i18n.t_in(athlete_lang, "coach.tg.coach_left_programs", coach="Марина").split("\n\n")[1] in body
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        (i18n.t_in(athlete_lang, "btn.programs"), "rt:manage")
    ]
    [banner] = banners
    assert banner["user_id"] == ATHLETE and banner["title"] == title
    assert banner["route"] == {"screen": "program", "program_id": program_id}
    assert banner["category"] == "coach_unlinked"
    if athlete_lang == "en":
        assert not i18n_coverage.has_cyrillic(body.replace("Марина", ""))
    [row] = await fresh_db.list_inbox(ATHLETE, athlete_lang)
    assert row["category"] == "coach_unlinked" and row["title"] == title

    # Тренеру серой строки нет: он убрал сам и знает.
    shown = await _tap(COACH, "coach:clients:0")
    assert "Маша" not in _screen(shown)


async def test_without_programs_the_athlete_is_not_told_about_programs(fresh_db, banners):
    await _linked(fresh_db)
    # Программу назначили, а атлет её удалил — утверждать «остались» нельзя.
    program_id = await _assign_program(fresh_db)
    await fresh_db.delete_program_by_id(program_id)
    assert await fresh_db.list_coach_assignments(COACH, ATHLETE) == []
    banners.clear()
    coach_api = await _api(fresh_db, COACH)
    resp = await coach_api.delete(f"/coach/clients/{ATHLETE}")
    assert resp.status_code == 200, resp.text
    [banner] = banners
    assert banner["user_id"] == ATHLETE and banner["title"] == "Тренер больше не ведёт тебя"
    assert banner["body"] == i18n.t_in("ru", "coach.push.coach_left_body", coach="Марина")
    assert "Программ" not in banner["body"]
    assert banner.get("route") is None
    [row] = await fresh_db.list_inbox(ATHLETE, "ru")
    assert row["category"] == "coach_unlinked"
    assert (await coach_api.get("/coach/clients")).json()["recently_unlinked"] == []
    await coach_api.aclose()


async def test_unlink_delivery_never_raises(fresh_db, monkeypatch):
    await _linked(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(fresh_db, "get_push_tokens", AsyncMock(side_effect=RuntimeError("db is gone")))
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Forbidden: bot was blocked"))
    assert await coach_notify.deliver_unlinked(
        COACH, ATHLETE, by=fresh_db.COACH_UNLINK_BY_CLIENT, client_name="Маша", bot=bot
    ) == (0, False)
    assert await coach_notify.deliver_unlinked(
        999_999, ATHLETE, by=fresh_db.COACH_UNLINK_BY_CLIENT, client_name="Маша", bot=bot
    ) == (0, False)
