"""Сервис для тренеров на всех сочетаниях платформ: тренер × атлет ∈
{T, A, B} — 9 пар, у B каждой стороны — оба входа (бот и REST /v1).

- T — только Telegram: бот, без iOS-устройства;
- A — только приложение: Sign in with Apple, синтетический отрицательный id,
  `telegram_linked = 0`, пуши APNs;
- B — оба: телеграмный аккаунт, вошедший в приложение, с APNs-токеном.

Каждая сторона действует своим каналом (T — хендлеры бота, A — REST, B — тот
или другой по параметру) и проходит путь целиком: тренер включает режим и
получает приглашение → атлет принимает кодом, тренеру «новый клиент» по
всем его каналам → пишет тренировку → тренер
видит клиента и тренировку (имена — на языке тренера) → комментирует →
атлет получает комментарий по всем своим каналам и видит его под тренировкой →
тренер назначает программу, атлет получает её по всем своим каналам →
AI-сводка тренеру по правилу `_background_ai_allowed` → атлет закрывает доступ
→ снова принимает по ссылке → тренер убирает клиента → доступа нет нигде.

Telegram и APNs подменены целиком: каждое сообщение и каждый банер записан
с адресатом, и тест сверяет маршрут дословно — ни одной отправки на
синтетический id, ни одного банера без токена.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import escape
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import aiogram
import httpx
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import ai_trainer
import api_v1
import apns
import coach_invite_page
import coach_service
import config
import i18n
import i18n_coverage
import seed_data
from handlers import coach, history, routines
from handlers import sharing as sharing_handlers

pytestmark = pytest.mark.asyncio

BENCH = "Жим штанги лёжа"  # идентичность каталога — имя на экране у каждого своё
PUBLIC = "https://training-log.example.com"
BOT_USERNAME = "TrainLogBot"
SUMMARY = "SUMMARY-OK"

COACH_NAME = {"ru": "Тренер Миша", "en": "Coach Mike"}
ATHLETE_NAME = {"ru": "Маша", "en": "Masha"}
COMMENT = {"ru": "Держи темп", "en": "Keep the pace"}
PROGRAM = {"ru": "Сила пять на пять", "en": "Strength five by five"}


# ---------- подменённые каналы доставки ----------


class FakeTelegram:
    """Один Bot на всё: и тот, что у хендлера (callback.bot), и
    короткоживущий REST-слоя (coach_notify._send_with_own_bot)."""

    def __init__(self):
        self.sent: list[tuple[str, int, str, Any]] = []
        self.session = SimpleNamespace(close=AsyncMock())
        self._next_id = 1000

    async def get_me(self):
        return SimpleNamespace(username=BOT_USERNAME)

    async def _record(self, kind, chat_id, payload, kwargs):
        self.sent.append((kind, chat_id, payload, kwargs.get("reply_markup")))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_message(self, chat_id, text, **kwargs):
        return await self._record("message", chat_id, text, kwargs)

    async def send_photo(self, chat_id, photo, **kwargs):
        return await self._record("photo", chat_id, kwargs.get("caption"), kwargs)


@pytest.fixture
def telegram(monkeypatch):
    bot = FakeTelegram()
    monkeypatch.setattr(config, "BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(aiogram, "Bot", lambda *a, **kw: bot)
    return bot


@pytest.fixture
def push(monkeypatch):
    alerts: list[dict] = []

    async def send_alert(user_id, token, title, body, **kwargs):
        alerts.append({"user_id": user_id, "token": token, "title": title, "body": body, **kwargs})
        return True

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", send_alert)
    return alerts


class _FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                prompt_tokens_details=None, completion_tokens_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=SUMMARY))], usage=usage)


@pytest.fixture
def fake_model(monkeypatch):
    completions = _FakeCompletions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: fake)
    # Разбор тренировки AI-тренером здесь не проверяется — он шёл бы в ту же
    # подменённую модель фоном и путал счёт вызовов сводки.
    monkeypatch.setattr(ai_trainer, "ensure_workout_comment", AsyncMock(return_value=None))
    return completions


@pytest.fixture(autouse=True)
def _public_url(monkeypatch):
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")


# ---------- бот: апдейты от имени человека ----------


@dataclass
class Account:
    kind: str  # T / A / B
    channel: str  # bot / rest
    uid: int
    lang: str
    api: Optional[httpx.AsyncClient] = None
    token: Optional[str] = None
    storage: MemoryStorage = field(default_factory=MemoryStorage)

    @property
    def has_telegram(self) -> bool:
        return self.kind in ("T", "B")

    @property
    def has_ios(self) -> bool:
        return self.kind in ("A", "B")

    def state(self) -> FSMContext:
        return FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=self.uid, user_id=self.uid))

    @contextmanager
    def lang_ctx(self):
        # Middleware бота (main.py) ставит язык атлета на каждый апдейт.
        with i18n.use_lang(self.lang):
            yield


def _callback(acc: Account, data: str, bot: FakeTelegram, first_name: Optional[str] = None):
    message = MagicMock()
    message.text = "previous screen"
    message.chat = SimpleNamespace(id=acc.uid)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2, delete=AsyncMock()))
    message.reply = AsyncMock()
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=acc.uid, username="u", language_code=acc.lang, first_name=first_name)
    callback.message = message
    callback.bot = bot
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _message(acc: Account, text: str, bot: FakeTelegram):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=acc.uid)
    msg.from_user = SimpleNamespace(id=acc.uid, username="u", language_code=acc.lang, first_name=None)
    msg.text = text
    msg.bot = bot
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=3))
    return msg


async def _tap(acc: Account, data: str, bot: FakeTelegram):
    assert acc.uid > 0, "кнопку в боте жмёт только тот, у кого есть Telegram"
    callback = _callback(acc, data, bot)
    with acc.lang_ctx():
        result = await coach.router.callback_query.trigger(callback, state=acc.state())
    assert result is not UNHANDLED, f"кнопку {data} не забрал никто"
    return callback


def _screen(callback) -> str:
    return callback.message.answer.await_args.args[0]


def _kb(callback):
    return callback.message.answer.await_args.kwargs.get("reply_markup")


def _alert(callback) -> str:
    args = callback.answer.await_args
    return args.args[0] if args and args.args else ""


def _buttons(markup) -> list[tuple[str, Optional[str]]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# ---------- аккаунты ----------


_ids = itertools.count(7001)


async def _account(db, kind: str, channel: str, lang: str) -> Account:
    if kind == "A":
        row = await db.create_app_only_user(language_code=lang)
        uid = row["telegram_id"]
        assert uid < 0 and not row["telegram_linked"]
    else:
        uid = next(_ids)
        await db.get_or_create_user(uid, f"user{uid}", lang)
    await db.set_user_lang(uid, lang)
    acc = Account(kind=kind, channel=channel, uid=uid, lang=lang)
    if kind in ("A", "B"):
        acc.token = f"apns-{kind}-{abs(uid)}"
        await db.register_push_token(uid, "ios", acc.token)
        api_token = await db.issue_api_token(uid)
        acc.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
        acc.api.headers["Authorization"] = f"Bearer {api_token}"
    return acc


# ---------- сторона тренера ----------


async def coach_become(acc: Account, bot) -> tuple[str, str]:
    """(код, ссылка, которую тренер раздаёт)."""
    name = COACH_NAME[acc.lang]
    if acc.channel == "rest":
        state = (await acc.api.post("/coach", json={"display_name": name})).json()
        assert state["is_coach"] is True and state["display_name"] == name
        return state["invite"]["code"], state["invite"]["app_link"]
    await _tap(acc, "coach:become", bot)
    named = _message(acc, name, bot)
    with acc.lang_ctx():
        await coach.coach_named(named, acc.state())
    text = named.answer.await_args.args[0]
    code = text.split("<code>")[1].split("</code>")[0]
    link = f"{PUBLIC}/coach/{code}"
    assert link in text
    return code, link


async def coach_client_names(acc: Account, bot) -> str:
    if acc.channel == "rest":
        return " ".join(c["name"] for c in (await acc.api.get("/coach/clients")).json()["clients"])
    return _screen(await _tap(acc, "coach:clients:0", bot))


async def coach_card(acc: Account, client_id: int, bot) -> str:
    if acc.channel == "rest":
        resp = await acc.api.get(f"/coach/clients/{client_id}")
        assert resp.status_code == 200, resp.text
        workouts = await acc.api.get(f"/coach/clients/{client_id}/workouts")
        assert workouts.status_code == 200, workouts.text
        return resp.text + workouts.text
    card = await _tap(acc, f"coach:cl:{client_id}", bot)
    return _screen(card) + " " + " ".join(t for t, _ in _buttons(_kb(card)))


async def coach_workout(acc: Account, client_id: int, wid: int, bot) -> str:
    if acc.channel == "rest":
        resp = await acc.api.get(f"/coach/clients/{client_id}/workouts/{wid}")
        assert resp.status_code == 200, resp.text
        return resp.text
    return _screen(await _tap(acc, f"coach:w:{client_id}:{wid}", bot))


async def coach_comment(acc: Account, client_id: int, wid: int, text: str, bot) -> None:
    if acc.channel == "rest":
        resp = await acc.api.post(f"/coach/clients/{client_id}/workouts/{wid}/comments", json={"text": text})
        assert resp.status_code == 201, resp.text
        return
    await _tap(acc, f"coach:cm:{client_id}:{wid}", bot)
    written = _message(acc, text, bot)
    with acc.lang_ctx():
        await coach.coach_comment_written(written, acc.state())
    sent = await _tap(acc, "coach:cms", bot)
    assert i18n.t_in(acc.lang, "coach.comment.sent", name="X").split("X")[0] in _screen(sent)


async def coach_summary(acc: Account, bot) -> tuple[Optional[str], int]:
    """(текст сводки или None, сколько клиентов попало в данные модели)."""
    if acc.channel == "rest":
        resp = await acc.api.post("/coach/ai-summary", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return body["text"], body["clients"]
    callback = await _tap(acc, "coach:ai", bot)
    text = callback.message.answer.await_args_list[-1].args[0]
    if SUMMARY in text:
        return text, 1
    assert i18n.t_in(acc.lang, "coach.summary_none") in text, text
    return None, 0


async def coach_remove(acc: Account, client_id: int, bot) -> None:
    if acc.channel == "rest":
        resp = await acc.api.delete(f"/coach/clients/{client_id}")
        assert resp.status_code == 200, resp.text
        return
    await _tap(acc, f"coach:rm:{client_id}", bot)
    await _tap(acc, f"coach:rmy:{client_id}", bot)


async def coach_has_no_access(acc: Account, client_id: int, wid: int, bot) -> None:
    if acc.channel == "rest":
        assert (await acc.api.get("/coach/clients")).json()["clients"] == []
        for path in (f"/coach/clients/{client_id}", f"/coach/clients/{client_id}/workouts",
                     f"/coach/clients/{client_id}/workouts/{wid}"):
            resp = await acc.api.get(path)
            assert resp.status_code == 404 and resp.json()["error"] == "not_linked", path
        resp = await acc.api.post(f"/coach/clients/{client_id}/workouts/{wid}/comments", json={"text": "x"})
        assert resp.status_code == 404
        return
    refused = i18n.t_in(acc.lang, "api.error.not_linked")
    for data in (f"coach:cl:{client_id}", f"coach:w:{client_id}:{wid}", f"coach:cm:{client_id}:{wid}"):
        callback = await _tap(acc, data, bot)
        assert _alert(callback) == refused, data
    listing = _screen(await _tap(acc, "coach:clients:0", bot))
    assert i18n.t_in(acc.lang, "coach.clients.empty") in listing


# ---------- сторона атлета ----------


async def athlete_accept_by_code(acc: Account, code: str, bot) -> None:
    name = ATHLETE_NAME[acc.lang]
    if acc.channel == "rest":
        preview = (await acc.api.get(f"/coach/invites/{code}")).json()
        assert preview["already_linked"] is False and preview["visibility"] == {"bodyweight": False}
        resp = await acc.api.post(f"/coach/invites/{code}/accept", json={"name": name})
        assert resp.status_code == 201, resp.text
        return
    await _tap(acc, "coach:code", bot)
    typed = _message(acc, code.lower(), bot)
    with acc.lang_ctx():
        await coach.coach_code_entered(typed, acc.state())
    assert i18n.t_in(acc.lang, "coach.btn.allow") in [
        t for t, _ in _buttons(typed.answer.await_args.kwargs["reply_markup"])
    ]
    await _bot_allow_and_name(acc, code, bot)


async def athlete_accept_by_link(acc: Account, link: str, bot) -> None:
    """Ссылка, которую раздал тренер (https-страница приглашения). Приложение
    открывает её Universal Link'ом и берёт код из пути; Telegram — кнопкой
    «Продолжить в Telegram» страницы, `/start coach_<код>`."""
    name = ATHLETE_NAME[acc.lang]
    code = coach_service.normalize_invite_code(link)
    if acc.channel == "rest":
        assert link == f"{PUBLIC}/coach/{code}"
        resp = await acc.api.post(f"/coach/invites/{code}/accept", json={"name": name})
        assert resp.status_code == 201, resp.text
        return
    # Страница приглашения ведёт в бота той же ссылкой, что и у тренера-бота.
    page = coach_invite_page.render_invite(acc.lang, "X", code, BOT_USERNAME)
    deep = coach_service.telegram_link(BOT_USERNAME, code)
    assert deep in page
    payload = deep.split("?start=", 1)[1]
    msg = _message(acc, f"/start {payload}", bot)
    with acc.lang_ctx():
        await coach.open_coach_invite(msg, CommandObject(command="start", args=payload), acc.state())
    assert i18n.t_in(acc.lang, "coach.btn.allow") in [t for t, _ in _buttons(msg.answer.await_args.kwargs["reply_markup"])]
    await _bot_allow_and_name(acc, code, bot)


async def _bot_allow_and_name(acc: Account, code: str, bot) -> None:
    await _tap(acc, f"coach:ok:{code}:0", bot)
    named = _message(acc, ATHLETE_NAME[acc.lang], bot)
    with acc.lang_ctx():
        await coach.coach_self_named(named, acc.state())
    head = i18n.t_in(acc.lang, "coach.linked_done", name="X").split("X")[0]
    assert head in named.answer.await_args.args[0]


async def athlete_log_workout(acc: Account, db) -> int:
    """Тренировка с упражнением из каталога: у атлета оно под именем на его
    языке, тренер должен увидеть его на своём."""
    if acc.channel == "rest":
        templates = (await acc.api.get("/exercise-templates", params={"query": seed_data.localized_exercise_name(BENCH, acc.lang)})).json()
        template = next(t for t in templates if t["name"] == seed_data.localized_exercise_name(BENCH, acc.lang))
        ex = (await acc.api.post(f"/exercise-templates/{template['id']}/add")).json()["id"]
        wid = (await acc.api.post("/workouts/active", json={})).json()["id"]
        resp = await acc.api.post(f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 80, "reps": 5})
        assert resp.status_code == 201, resp.text
        assert (await acc.api.post(f"/workouts/{wid}/finish", json={})).status_code == 200
        return wid
    # Бот пишет тренировку теми же вызовами базы, что живой трекер.
    ex = await db.get_or_create_user_exercise_by_name(acc.uid, BENCH)
    assert (await db.get_exercise(ex))["display_name"] == seed_data.localized_exercise_name(BENCH, acc.lang)
    wid = await db.create_workout(acc.uid)
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, 80.0, 5)
    await db.finish_workout(wid)
    return wid


async def athlete_sees_comment(acc: Account, wid: int, text: str, coach_name: str, bot) -> None:
    if acc.channel == "rest":
        comments = (await acc.api.get(f"/workouts/{wid}/coach-comments")).json()["comments"]
        assert [(c["text"], c["coach_name"]) for c in comments] == [(text, coach_name)]
        return
    await athlete_opens_from_telegram(acc, wid, text, coach_name, bot)


async def athlete_opens_from_telegram(acc: Account, wid: int, text: str, coach_name: str, bot) -> None:
    """Кнопка «Открыть тренировку» под сообщением о комментарии — карточка из
    истории с блоком тренера."""
    callback = _callback(acc, f"hist:item:{wid}", bot)
    with acc.lang_ctx():
        result = await history.router.callback_query.trigger(callback, state=acc.state())
    assert result is not UNHANDLED
    screen = _screen(callback)
    assert text in screen and coach_name in screen
    assert i18n.t_in(acc.lang, "coach.card.header_named", name=escape(coach_name)) in screen


async def athlete_consent(acc: Account) -> None:
    resp = await acc.api.patch("/settings", json={"ai_consent": True})
    assert resp.status_code == 200, resp.text


async def athlete_revoke(acc: Account, coach_id: int, bot) -> None:
    if acc.channel == "rest":
        resp = await acc.api.delete(f"/coach-links/{coach_id}")
        assert resp.status_code == 200, resp.text
        return
    await _tap(acc, f"coach:rv:{coach_id}", bot)
    done = await _tap(acc, f"coach:rvy:{coach_id}", bot)
    assert i18n.t_in(acc.lang, "coach.access_closed", name="X").split("X")[0] in _screen(done)


async def athlete_has_no_coach(acc: Account, bot) -> None:
    if acc.channel == "rest":
        assert (await acc.api.get("/coach-links")).json()["coaches"] == []
        return
    hub = _screen(await _tap(acc, "coach:hub", bot))
    assert i18n.t_in(acc.lang, "coach.hub.no_coach") in hub


# ---------- доставка тренерских событий ----------


def assert_delivered(
    acc: Account, telegram: FakeTelegram, push: list[dict], *,
    tg_key: str, tg_params: dict, button: tuple[str, str], title_key: str, route: dict, data: tuple[str, ...],
) -> None:
    """Событие доехало до `acc` по каждому его каналу и только по ним: одно
    сообщение в Telegram (если он есть) и один банер (если есть iPhone), на
    языке `acc`; данные (имена) — как есть."""
    if acc.has_telegram:
        assert len(telegram.sent) == 1, telegram.sent
        kind, chat_id, body, kb = telegram.sent[0]
        assert kind == "message" and chat_id == acc.uid
        head = i18n.t_in(acc.lang, tg_key, **{k: "" for k in tg_params}).split("\n")[0]
        assert head in body, body
        for d in data:
            assert d in body, body
        assert _buttons(kb) == [button]
        if acc.lang == "en":
            assert not i18n_coverage.has_cyrillic(_strip(body, *data)), body
    else:
        assert telegram.sent == []
    if acc.has_ios:
        assert len(push) == 1, push
        alert = push[0]
        assert alert["user_id"] == acc.uid and alert["token"] == acc.token
        assert alert["title"] == i18n.t_in(acc.lang, title_key)
        for d in data:
            assert d in alert["body"], alert
        assert alert["route"] == route
        if acc.lang == "en":
            assert not i18n_coverage.has_cyrillic(_strip(alert["body"], *data)), alert
    else:
        assert push == []
    assert all(chat_id > 0 for _k, chat_id, _b, _m in telegram.sent)


async def coach_assign(acc: Account, client_id: int, program_id: int, bot) -> None:
    if acc.channel == "rest":
        resp = await acc.api.post(f"/coach/clients/{client_id}/programs", json={"program_id": program_id})
        assert resp.status_code == 201, resp.text
        return
    done = await _tap(acc, f"coach:asp:{client_id}:{program_id}", bot)
    assert i18n.t_in(acc.lang, "coach.program_assigned", name="X").split("X")[0] in _screen(done)


async def athlete_opens_program_from_telegram(acc: Account, program_id: int, name: str, bot) -> None:
    """Кнопка «Открыть программу» под сообщением — экран этой программы."""
    callback = _callback(acc, f"rt:prg:{program_id}", bot)
    with acc.lang_ctx():
        result = await routines.router.callback_query.trigger(callback, state=acc.state())
    assert result is not UNHANDLED
    shown = callback.message.answer.await_args or callback.message.edit_text.await_args
    assert name in shown.args[0]


# ---------- матрица ----------


def _sides(kind: str) -> list[str]:
    return {"T": ["bot"], "A": ["rest"], "B": ["bot", "rest"]}[kind]


COMBOS = [
    pytest.param(ck, cch, ak, ach, id=f"coach{ck}-{cch}__athlete{ak}-{ach}")
    for ck in "TAB" for cch in _sides(ck)
    for ak in "TAB" for ach in _sides(ak)
]
LANGS = [pytest.param("en", "ru", id="coach_en-athlete_ru"), pytest.param("ru", "en", id="coach_ru-athlete_en")]


def _strip(text: str, *data: str) -> str:
    for d in data:
        text = text.replace(d, "")
    return text


@pytest.mark.parametrize("coach_lang, athlete_lang", LANGS)
@pytest.mark.parametrize("coach_kind, coach_channel, athlete_kind, athlete_channel", COMBOS)
async def test_coach_athlete_flow(
    fresh_db, telegram, push, fake_model,
    coach_kind, coach_channel, athlete_kind, athlete_channel, coach_lang, athlete_lang,
):
    db = fresh_db
    sharing_handlers._bot_username = BOT_USERNAME
    trainer = await _account(db, coach_kind, coach_channel, coach_lang)
    athlete = await _account(db, athlete_kind, athlete_channel, athlete_lang)
    coach_name = COACH_NAME[coach_lang]
    athlete_name = ATHLETE_NAME[athlete_lang]

    # 1. Тренер включает режим и получает приглашение: код и https-ссылку.
    code, link = await coach_become(trainer, telegram)
    assert len(code) == coach_service.INVITE_LENGTH
    assert link == f"{PUBLIC}/coach/{code}"

    # 2. Атлет принимает кодом.
    await athlete_accept_by_code(athlete, code, telegram)
    linked = await db.get_coach_link(trainer.uid, athlete.uid)
    assert linked is not None and linked["client_name"] == athlete_name
    assert not linked["show_bodyweight"]

    # 2a. Тренеру — «новый клиент» по каждому его каналу, на его языке.
    assert_delivered(
        trainer, telegram, push,
        tg_key="coach.tg.new_client", tg_params={"name": ""},
        button=(i18n.t_in(coach_lang, "coach.btn.my_clients_short"), "coach:clients:0"),
        title_key="coach.push.new_client_title", route={"screen": "coach_clients"},
        data=(athlete_name,),
    )
    telegram.sent.clear()
    push.clear()

    # 4. Атлет пишет тренировку своим каналом; тренер видит его и её своим.
    wid = await athlete_log_workout(athlete, db)
    bench_for_coach = seed_data.localized_exercise_name(BENCH, coach_lang)
    bench_for_athlete = seed_data.localized_exercise_name(BENCH, athlete_lang)
    assert athlete_name in await coach_client_names(trainer, telegram)
    card = await coach_card(trainer, athlete.uid, telegram)
    assert athlete_name in card
    assert bench_for_coach in card, f"упражнение не на языке тренера: {card}"
    assert bench_for_athlete not in card
    detail = await coach_workout(trainer, athlete.uid, wid, telegram)
    assert bench_for_coach in detail and bench_for_athlete not in detail, detail
    if coach_lang == "en":
        for shown in (card, detail):
            assert not i18n_coverage.has_cyrillic(_strip(shown, athlete_name, coach_name)), shown

    # 3/5. Комментарий тренера доезжает до атлета по каждому его каналу и
    # только по ним. До комментария не уходило ничего.
    assert telegram.sent == [] and push == []
    text = COMMENT[coach_lang]
    await coach_comment(trainer, athlete.uid, wid, text, telegram)

    if athlete.has_telegram:
        assert len(telegram.sent) == 1, telegram.sent
        kind, chat_id, body, kb = telegram.sent[0]
        assert kind == "message" and chat_id == athlete.uid
        assert text in body and coach_name in body
        assert i18n.t_in(athlete_lang, "coach.tg.comment", coach="", date="", text="").split("\n")[0] in body
        assert _buttons(kb) == [(i18n.t_in(athlete_lang, "coach.btn.open_workout"), f"hist:item:{wid}")]
        if athlete_lang == "en":
            assert not i18n_coverage.has_cyrillic(_strip(body, coach_name, text)), body
    else:
        assert telegram.sent == []
    if athlete.has_ios:
        assert len(push) == 1, push
        alert = push[0]
        assert alert["user_id"] == athlete.uid and alert["token"] == athlete.token
        assert alert["title"] == i18n.t_in(athlete_lang, "coach.push.comment_title")
        assert coach_name in alert["body"] and text in alert["body"]
        assert alert["route"] == {"screen": "workout_detail", "workout_id": wid}
    else:
        assert push == []
    assert all(chat_id > 0 for _k, chat_id, _b, _m in telegram.sent)

    await athlete_sees_comment(athlete, wid, text, coach_name, telegram)
    if athlete.has_telegram:
        # Кнопка из сообщения в Telegram живая и у того, кто сидит в приложении.
        await athlete_opens_from_telegram(athlete, wid, text, coach_name, telegram)
    # Тренер видит свой комментарий под тренировкой.
    assert text in await coach_workout(trainer, athlete.uid, wid, telegram)

    # 5b. Тренер назначает программу — атлет получает её по каждому своему
    # каналу, на своём языке; кнопка и банер ведут в его копию программы.
    program_name = PROGRAM[coach_lang]
    source = await db.create_program(trainer.uid, program_name)
    day = await db.create_routine(trainer.uid, "A", program_id=source)
    await db.add_routine_exercise(day, await db.get_or_create_user_exercise_by_name(trainer.uid, BENCH), 0, "5×5")
    telegram.sent.clear()
    push.clear()
    await coach_assign(trainer, athlete.uid, source, telegram)
    copy = next(p for p in await db.list_programs(athlete.uid) if p["name"] == program_name)
    assert_delivered(
        athlete, telegram, push,
        tg_key="coach.tg.program", tg_params={"coach": "", "name": ""},
        button=(i18n.t_in(athlete_lang, "btn.open_program"), f"rt:prg:{copy['id']}"),
        title_key="coach.push.program_title", route={"screen": "program", "program_id": copy["id"]},
        data=(coach_name, program_name),
    )
    if athlete.has_telegram:
        await athlete_opens_program_from_telegram(athlete, copy["id"], program_name, telegram)

    # 6. AI-сводка: атлет приложения (A и B — есть iOS-токен или нет Telegram)
    # попадает в неё только с согласием, чисто телеграмный — сразу.
    summary, clients = await coach_summary(trainer, telegram)
    if athlete.has_ios:
        assert summary is None and clients == 0
        await athlete_consent(athlete)
        summary, clients = await coach_summary(trainer, telegram)
    assert summary is not None and SUMMARY in summary and clients == 1
    model_data = fake_model.calls[-1]["messages"][1]["content"]
    assert athlete_name in model_data and bench_for_coach in model_data, model_data
    assert fake_model.calls[-1]["messages"][0]["content"].endswith(i18n.t_in(coach_lang, "ai.language_tail"))

    # 7. Атлет закрывает доступ — тренер не видит ничего.
    await athlete_revoke(athlete, trainer.uid, telegram)
    await coach_has_no_access(trainer, athlete.uid, wid, telegram)
    await athlete_has_no_coach(athlete, telegram)

    # 2b. Снова — по ссылке, которую раздал тренер.
    await athlete_accept_by_link(athlete, link, telegram)
    assert await db.get_coach_link(trainer.uid, athlete.uid) is not None
    assert athlete_name in await coach_client_names(trainer, telegram)

    # 7b. Теперь убирает тренер.
    await coach_remove(trainer, athlete.uid, telegram)
    await coach_has_no_access(trainer, athlete.uid, wid, telegram)
    await athlete_has_no_coach(athlete, telegram)

    # За весь путь — ни одной отправки на синтетический id и ни одного банера
    # без токена.
    assert all(chat_id > 0 for _k, chat_id, _b, _m in telegram.sent), telegram.sent
    tokens = {acc.token for acc in (trainer, athlete) if acc.token}
    assert all(a["token"] in tokens for a in push), push
    for acc in (trainer, athlete):
        if acc.api is not None:
            await acc.api.aclose()
