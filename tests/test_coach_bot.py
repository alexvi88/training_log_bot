"""Сервис для тренеров в боте (handlers/coach.py, coach_notify.py): ссылка-
приглашение и превью с переключателями, «Разрешить» с именем, «Мой тренер» и
отзыв доступа, «Мои клиенты» и карточка клиента, комментарий тренера с
сообщением атлету на его языке, назначение программы, AI-сводка с подменённой
моделью и отказ всем, у кого нет связки."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import legacy_food
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery

import acquisition
import ai_limits
import ai_trainer
import apns
import coach_notify
import coach_service
import i18n
import i18n_coverage
from fsm import CoachFlow
from handlers import coach, history

pytestmark = pytest.mark.asyncio

COACH = 601
ATHLETE = 602
STRANGER = 603


def _bot():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username="TrainLogBot"))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    return bot


def _callback(user_id: int, data: str, *, bot=None, first_name: str | None = None):
    message = MagicMock()
    message.text = "previous screen"
    message.chat = SimpleNamespace(id=user_id)
    message.message_id = 1
    message.edit_text = AsyncMock(return_value=message)
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=2, delete=AsyncMock()))
    message.reply = AsyncMock()
    message.delete = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None, first_name=first_name)
    callback.message = message
    callback.bot = bot or _bot()
    callback.data = data
    callback.answer = AsyncMock()
    return callback


def _message(user_id: int, text: str, *, language_code: str | None = None, bot=None):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=user_id)
    msg.from_user = SimpleNamespace(id=user_id, username="someone", language_code=language_code)
    msg.text = text
    msg.bot = bot or _bot()
    msg.answer = AsyncMock(return_value=SimpleNamespace(message_id=3))
    return msg


_STORAGE = {"storage": MemoryStorage()}


@pytest.fixture(autouse=True)
def _fresh_storage():
    """Состояние FSM — своё у каждого теста: id атлета и тренера общие."""
    _STORAGE["storage"] = MemoryStorage()


def _state(user_id: int) -> FSMContext:
    return FSMContext(storage=_STORAGE["storage"], key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


async def _tap(user_id: int, data: str, **kwargs):
    """Нажать кнопку через настоящий роутер — так ловятся и пересечения
    префиксов callback_data, а не только сам обработчик."""
    callback = _callback(user_id, data, **kwargs)
    result = await coach.router.callback_query.trigger(callback, state=_state(user_id))
    assert result is not UNHANDLED, f"кнопку {data} не забрал никто"
    return callback


def _screen(callback) -> str:
    return callback.message.answer.await_args.args[0]


def _kb(callback):
    return callback.message.answer.await_args.kwargs.get("reply_markup")


def _buttons(markup) -> list[tuple[str, str | None]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _alert(callback) -> str:
    args = callback.answer.await_args
    return args.args[0] if args and args.args else ""


async def _user(db, telegram_id: int, lang: str = "ru"):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    await db.set_user_lang(telegram_id, lang)


async def _coach_with_invite(db, name: str = "Иван Петров") -> str:
    await _user(db, COACH)
    await db.upsert_coach_profile(COACH, name)
    await db.replace_coach_invite(COACH, "K7MP2QXA")
    return "K7MP2QXA"


async def _linked(db, *, athlete_lang: str = "ru", bw: bool = False):
    code = await _coach_with_invite(db)
    await _user(db, ATHLETE, athlete_lang)
    assert await db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=bw)
    return code


async def _finished_workout(db, user_id: int, exercise: str = "Жим лёжа", weight: float = 80.0, started_at=None):
    """Законченная тренировка с одним подходом; упражнение с тем же именем
    берётся то же самое — так вторая тренировка становится рекордом."""
    cur = await db.conn().execute(
        "SELECT id FROM exercises WHERE user_id = ? AND name = ?", (user_id, exercise)
    )
    row = await cur.fetchone()
    if row is not None:
        ex = row["id"]
    else:
        gid = await db.create_muscle_group(user_id, "Грудь")
        ex = await db.create_exercise(user_id, exercise, gid)
    wid = await db.create_workout(user_id, started_at=started_at) if started_at else await db.create_workout(user_id)
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, weight, 5)
    await db.finish_workout(wid, finished_at=started_at) if started_at else await db.finish_workout(wid)
    return wid


def _days_ago(n: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=n)).isoformat(timespec="seconds")


# ---------- атлет: ссылка, превью, «Разрешить» ----------


async def test_deep_link_preview_toggles_and_accept_with_typed_name(fresh_db):
    code = await _coach_with_invite(fresh_db)
    msg = _message(ATHLETE, f"/start coach_{code}", language_code="ru")
    await coach.open_coach_invite(msg, CommandObject(command="start", args=f"coach_{code.lower()}"), _state(ATHLETE))

    # Новичок по ссылке тренера: заведён, источник — код тренера с автором.
    user = await fresh_db.get_user(ATHLETE)
    assert user["source"] == acquisition.SOURCE_COACH_INVITE and user["referrer_id"] == COACH
    text = msg.answer.await_args_list[-1].args[0]
    assert "Иван Петров хочет вести тебя как тренер" in text
    assert "Не увидит никогда" in text and "Apple ID" in text
    buttons = _buttons(msg.answer.await_args_list[-1].kwargs["reply_markup"])
    assert ("⚖️ Вес тела: закрыт", f"coach:inv:{code}:1") in buttons
    assert ("✅ Разрешить", f"coach:ok:{code}:0") in buttons
    # Переключатель у превью один — вес тела.
    assert [t for t, _cb in buttons if t.startswith("⚖️")] == ["⚖️ Вес тела: закрыт"]
    assert len([cb for _t, cb in buttons if cb.startswith("coach:inv:")]) == 1
    # Превью ничего не открывает.
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None

    toggled = await _tap(ATHLETE, f"coach:inv:{code}:1")
    assert ("⚖️ Вес тела: открыт", f"coach:inv:{code}:0") in _buttons(_kb(toggled))
    assert ("✅ Разрешить", f"coach:ok:{code}:1") in _buttons(_kb(toggled))

    allow = await _tap(ATHLETE, f"coach:ok:{code}:1", first_name="Мария")
    assert "Как тебя назвать тренеру" in _screen(allow)
    assert ("Назваться «Мария»", "coach:nm") in _buttons(_kb(allow))
    state = _state(ATHLETE)
    assert await state.get_state() == CoachFlow.naming_self.state

    named = _message(ATHLETE, "  Маша  ")
    await coach.coach_self_named(named, state)
    link = await fresh_db.get_coach_link(COACH, ATHLETE)
    assert link["client_name"] == "Маша"
    assert bool(link["show_bodyweight"]) is True
    assert "Готово — Иван Петров видит твой дневник" in named.answer.await_args.args[0]
    assert await state.get_state() is None


async def test_accept_with_telegram_first_name_button(fresh_db):
    code = await _coach_with_invite(fresh_db)
    await _user(fresh_db, ATHLETE)
    await _tap(ATHLETE, f"coach:ok:{code}:1", first_name="Мария")
    done = await _tap(ATHLETE, "coach:nm", first_name="Мария")
    link = await fresh_db.get_coach_link(COACH, ATHLETE)
    assert link["client_name"] == "Мария" and bool(link["show_bodyweight"]) is True
    assert "видит твой дневник" in _screen(done)


async def test_old_two_flag_buttons_still_work_and_read_only_body_weight(fresh_db):
    """Кнопки первой версии в уже отправленных сообщениях несли два разряда
    (`10`, `01`): первый — вес тела, второй больше ничего не значит."""
    code = await _coach_with_invite(fresh_db)
    await _user(fresh_db, ATHLETE)
    preview = await _tap(ATHLETE, f"coach:inv:{code}:01")
    assert ("⚖️ Вес тела: закрыт", f"coach:inv:{code}:1") in _buttons(_kb(preview))
    await _tap(ATHLETE, f"coach:ok:{code}:01", first_name="Мария")
    await _tap(ATHLETE, "coach:nm", first_name="Мария")
    link = await fresh_db.get_coach_link(COACH, ATHLETE)
    assert bool(link["show_bodyweight"]) is False and not link["show_food"]


async def test_typed_code_bad_own_and_already_linked(fresh_db):
    code = await _coach_with_invite(fresh_db)
    await _user(fresh_db, ATHLETE)
    state = _state(ATHLETE)
    await _tap(ATHLETE, "coach:code")
    assert await state.get_state() == CoachFlow.entering_code.state

    bad = _message(ATHLETE, "NOPE2345")
    await coach.coach_code_entered(bad, state)
    assert "Такого кода нет" in bad.answer.await_args.args[0]
    assert await state.get_state() == CoachFlow.entering_code.state  # можно ввести ещё раз

    # Ссылка целиком (телеграмная или приложения) — тоже код.
    for raw in (f"https://t.me/TrainLogBot?start=coach_{code}", f"trainingdiary://coach/{code.lower()}"):
        ok = _message(ATHLETE, raw)
        await coach.coach_code_entered(ok, state)
        assert "хочет вести тебя как тренер" in ok.answer.await_args.args[0], raw

    own = _message(COACH, code)
    await coach.coach_code_entered(own, _state(COACH))
    assert "собственный код" in own.answer.await_args.args[0]

    await fresh_db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=False)
    again = _message(ATHLETE, code)
    await coach.coach_code_entered(again, state)
    assert "уже видит твой дневник" in again.answer.await_args.args[0]


async def test_not_now_opens_nothing(fresh_db):
    code = await _coach_with_invite(fresh_db)
    await _user(fresh_db, ATHLETE)
    await _tap(ATHLETE, f"coach:ok:{code}:0")
    declined = await _tap(ATHLETE, "coach:no")
    assert "Ничего не открыл" in _screen(declined)
    assert await _state(ATHLETE).get_state() is None
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None


async def test_coach_payload_is_its_own_acquisition_source():
    assert acquisition.parse_start_payload("coach_K7MP2QXA").source == acquisition.SOURCE_COACH_INVITE


# ---------- атлет: «Мой тренер» ----------


async def test_my_coach_toggles_and_revoke(fresh_db):
    await _linked(fresh_db)
    hub = await _tap(ATHLETE, "coach:hub")
    assert "Иван Петров" in _screen(hub) and "знает тебя как «Маша»" in _screen(hub)
    assert ("🧑‍🏫 Иван Петров", f"coach:my:{COACH}") in _buttons(_kb(hub))

    screen = await _tap(ATHLETE, f"coach:my:{COACH}")
    assert "никогда" in _screen(screen)
    # Старая кнопка второго переключателя из уже отправленного сообщения —
    # ничего не меняет, только перерисовывает экран.
    stale = await _tap(ATHLETE, f"coach:myt:{COACH}:food")
    assert not (await fresh_db.get_coach_link(COACH, ATHLETE))["show_food"]
    assert [cb for _t, cb in _buttons(_kb(stale)) if cb.startswith("coach:myt:")] == [f"coach:myt:{COACH}:bw"]
    toggled = await _tap(ATHLETE, f"coach:myt:{COACH}:bw")
    link = await fresh_db.get_coach_link(COACH, ATHLETE)
    assert bool(link["show_bodyweight"]) is True
    assert ("⚖️ Вес тела: открыт", f"coach:myt:{COACH}:bw") in _buttons(_kb(toggled))

    ask = await _tap(ATHLETE, f"coach:rv:{COACH}")
    assert "останутся у тебя" in _screen(ask)
    done = await _tap(ATHLETE, f"coach:rvy:{COACH}")
    assert "Закрыл доступ: Иван Петров" in _screen(done)
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None


# ---------- тренер: «Я тренер», клиенты, карточка ----------


async def test_become_coach_shows_code_and_deep_link(fresh_db):
    await _user(fresh_db, COACH)
    state = _state(COACH)
    await _tap(COACH, "coach:become")
    assert await state.get_state() == CoachFlow.naming_coach.state
    named = _message(COACH, " Иван  Петров ")
    await coach.coach_named(named, state)
    assert (await fresh_db.get_coach_profile(COACH))["display_name"] == "Иван Петров"
    code = (await fresh_db.get_coach_invite_for(COACH))["code"]
    text = named.answer.await_args.args[0]
    assert f"https://t.me/TrainLogBot?start=coach_{code}" in text and f"<code>{code}</code>" in text
    share = next(b for row in named.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row if b.url)
    assert share.url.startswith("https://t.me/share/url?url=")

    renewed = await _tap(COACH, "coach:renewy")
    new_code = (await fresh_db.get_coach_invite_for(COACH))["code"]
    assert new_code != code and "старый больше не открывается" in _screen(renewed)
    assert await fresh_db.get_coach_invite(code) is None


async def test_invite_screen_leads_with_https_page(fresh_db, monkeypatch):
    """Главная ссылка приглашения — https-страница: схему trainingdiary://
    мессенджеры не открывают, а без приложения она не делает ничего."""
    from urllib.parse import unquote

    import config

    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")
    await _user(fresh_db, COACH)
    state = _state(COACH)
    await _tap(COACH, "coach:become")
    named = _message(COACH, "ABishop")
    await coach.coach_named(named, state)
    code = (await fresh_db.get_coach_invite_for(COACH))["code"]
    page = f"https://training-log.example.com/coach/{code}"
    text = named.answer.await_args.args[0]
    assert page in text and f"<code>{code}</code>" in text
    buttons = [b for row in named.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    share = next(b for b in buttons if b.url)
    copy = next(b for b in buttons if b.copy_text)
    assert unquote(share.url).startswith(f"https://t.me/share/url?url={page}&text=")
    assert page in copy.copy_text.text and code in copy.copy_text.text
    for shown in (text, unquote(share.url), copy.copy_text.text):
        assert "trainingdiary://" not in shown


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_share_text_is_short_one_link_code_on_its_own_line(fresh_db, monkeypatch, lang):
    """Приглашение — строка от тренера, одна ссылка и код отдельной строкой:
    ссылка из App Store до приложения не доезжает, код вставляют руками."""
    from urllib.parse import unquote

    import config

    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")
    await _user(fresh_db, COACH)
    await fresh_db.set_user_lang(COACH, lang)
    i18n.set_lang(lang)
    state = _state(COACH)
    await _tap(COACH, "coach:become")
    named = _message(COACH, "ABishop")
    await coach.coach_named(named, state)
    code = (await fresh_db.get_coach_invite_for(COACH))["code"]
    page = f"https://training-log.example.com/coach/{code}"
    buttons = [b for row in named.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    copied = next(b for b in buttons if b.copy_text).copy_text.text
    lines = copied.split("\n")
    assert len(lines) == 3 and lines[1] == page and lines[2].endswith(code)
    assert copied.count("http") == 1 and "ABishop" in lines[0]
    # Telegram сам ставит ссылку над текстом — в тексте её нет.
    shared = unquote(next(b for b in buttons if b.url).url)
    assert shared.count("http") == 2  # t.me/share и сама страница — один раз
    # Строку с кодом, вставленную в приложение или бота, разбор понимает.
    assert coach_service.normalize_invite_code(lines[2]) == code
    assert coach_service.normalize_invite_code(copied) == code


async def test_clients_list_signals_and_pagination(fresh_db):
    await _linked(fresh_db)
    # Маша тренировалась 9 дней назад — сигнал «пропал».
    await _finished_workout(fresh_db, ATHLETE, started_at=_days_ago(9))
    for i in range(coach.CLIENTS_PAGE_SIZE + 1):
        cid = 700 + i
        await _user(fresh_db, cid)
        await fresh_db.create_coach_link(COACH, cid, f"Атлет {i:02d}", show_bodyweight=False)

    first = await _tap(COACH, "coach:clients:0")
    text = _screen(first)
    # С предупреждением — первой строкой.
    assert text.index("Маша") < text.index("Атлет 00")
    assert "9 дней без зала" in text
    cbs = [cb for _t, cb in _buttons(_kb(first))]
    assert f"coach:cl:{ATHLETE}" in cbs and "coach:clients:1" in cbs and "coach:ai" in cbs
    second = await _tap(COACH, "coach:clients:1")
    assert "Атлет 08" in _screen(second)


async def test_client_card_and_read_only_workout_never_show_ai(fresh_db):
    await _linked(fresh_db)
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(20))
    wid = await _finished_workout(fresh_db, ATHLETE)
    await fresh_db.set_workout_ai_comment(wid, "СЕКРЕТНЫЙ-РАЗБОР-AI")

    card = await _tap(COACH, f"coach:cl:{ATHLETE}")
    text = _screen(card)
    assert "Маша" in text and "Рекорды за неделю" in text and "Жим лёжа" in text
    cbs = [cb for _t, cb in _buttons(_kb(card))]
    assert f"coach:w:{ATHLETE}:{wid}" in cbs
    # Сначала сводка и тренировки по неделям, потом программы и рекорды.
    assert text.index("Тренировки по неделям") < text.index("Рекорды за неделю")
    assert "За 12 недель — 2 тренировки" in text and "по эту неделю" in text
    # Действия тренера — первыми, разделы — за ними, «Убрать из клиентов» —
    # последней строкой и красной.
    assert cbs[:3] == [f"coach:asg:{ATHLETE}", f"coach:ai:{ATHLETE}", f"coach:pg:{ATHLETE}"]
    last = _kb(card).inline_keyboard[-1]
    assert len(last) == 1 and last[0].callback_data == f"coach:rm:{ATHLETE}" and last[0].style == "danger"
    # Вес тела закрыт — его кнопки нет вовсе.
    assert f"coach:bw:{ATHLETE}" not in cbs
    # И строки про вес в шапке тоже нет: подпись повторяла раздел.
    assert "Вес тела" not in text and "вес тела" not in text

    workout = await _tap(COACH, f"coach:w:{ATHLETE}:{wid}")
    assert "Жим лёжа" in _screen(workout)
    assert "СЕКРЕТНЫЙ" not in _screen(workout) and "user602" not in _screen(workout)

    hidden = await _tap(COACH, f"coach:bw:{ATHLETE}")
    assert "закрытым" in _alert(hidden)


async def test_client_card_week_bars_only_when_data_shows_a_trend(fresh_db):
    await _linked(fresh_db)
    # Пустой дневник — блока нет: плашка уже сказала, что тренировок нет.
    assert "Тренировки по неделям" not in _screen(await _tap(COACH, f"coach:cl:{ATHLETE}"))
    # Одна тренировка — частота без столбиков.
    await _finished_workout(fresh_db, ATHLETE)
    one = _screen(await _tap(COACH, f"coach:cl:{ATHLETE}"))
    assert "Тренировки по неделям" in one and "<code>" not in one
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(20))
    assert "<code>" in _screen(await _tap(COACH, f"coach:cl:{ATHLETE}"))


async def test_week_bars_scale_to_the_busiest_week_and_dot_for_empty():
    weeks = [{"workouts": n} for n in (0, 1, 2, 4)]
    assert coach.week_bars(weeks) == "·▃▅█"
    assert coach.week_bars([{"workouts": 0}] * 3) == "···"


async def test_bodyweight_only_when_the_athlete_opened_it(fresh_db):
    await _linked(fresh_db, bw=True)
    await fresh_db.add_bodyweight_log(ATHLETE, 70.5)
    await legacy_food.insert_food_row(ATHLETE, dt.date.today().isoformat(), "Гречка", calories=300)
    card = await _tap(COACH, f"coach:cl:{ATHLETE}")
    cbs = [cb for _t, cb in _buttons(_kb(card))]
    assert f"coach:bw:{ATHLETE}" in cbs
    assert not any(cb.startswith("coach:fd:") for cb in cbs)
    # Открытый вес — это кнопка «⚖️ Вес тела», без строки-дубля в шапке.
    assert "видишь вес тела" not in _screen(card)
    assert "Гречка" not in _screen(card) and "ккал" not in _screen(card)
    assert "70.5кг" in _screen(await _tap(COACH, f"coach:bw:{ATHLETE}"))
    # Кнопка первой версии карточки в уже отправленном сообщении открывает
    # саму карточку — дневника питания тренер не видит нигде.
    legacy = _screen(await _tap(COACH, f"coach:fd:{ATHLETE}"))
    assert "Маша" in legacy and "Гречка" not in legacy and "ккал" not in legacy


async def test_non_linked_access_is_refused_everywhere(fresh_db):
    await _linked(fresh_db)
    wid = await _finished_workout(fresh_db, ATHLETE)
    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Чужой")
    for data in (
        f"coach:cl:{ATHLETE}", f"coach:w:{ATHLETE}:{wid}", f"coach:ws:{ATHLETE}:0",
        f"coach:bw:{ATHLETE}", f"coach:fd:{ATHLETE}", f"coach:cm:{ATHLETE}:{wid}",
        f"coach:asg:{ATHLETE}", f"coach:rm:{ATHLETE}", f"coach:rmy:{ATHLETE}", f"coach:ai:{ATHLETE}",
    ):
        callback = await _tap(STRANGER, data)
        assert "не открыл тебе дневник" in _alert(callback), data
        callback.message.answer.assert_not_awaited()
    # Не тренер вовсе — «Мои клиенты» отправляют к «Я тренер».
    await _user(fresh_db, 604)
    refused = await _tap(604, "coach:clients:0")
    assert "Я тренер" in _alert(refused)
    # Атлет не подглядывает чужого «клиента» через свой id.
    assert "не открыл тебе дневник" in _alert(await _tap(ATHLETE, f"coach:cl:{COACH}"))
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is not None


async def test_remove_client_with_confirmation(fresh_db):
    await _linked(fresh_db)
    ask = await _tap(COACH, f"coach:rm:{ATHLETE}")
    assert "Вернуть доступ можно только новым приглашением" in _screen(ask)
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is not None
    done = await _tap(COACH, f"coach:rmy:{ATHLETE}")
    assert "Убрал <b>Маша</b>" in _screen(done)
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None


# ---------- комментарий: тренер пишет → атлет получает ----------


@pytest.mark.parametrize("athlete_lang, expected", [("ru", "КОММЕНТАРИЙ ТРЕНЕРА"), ("en", "YOUR COACH COMMENTED")])
async def test_comment_reaches_the_athlete_in_their_language(fresh_db, monkeypatch, athlete_lang, expected):
    await _linked(fresh_db, athlete_lang=athlete_lang)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    wid = await _finished_workout(fresh_db, ATHLETE)
    state = _state(COACH)

    prompt = await _tap(COACH, f"coach:cm:{ATHLETE}:{wid}")
    assert "Что скажешь атлету" in _screen(prompt)
    assert await state.get_state() == CoachFlow.writing_comment.state
    written = _message(COACH, "  Держи темп <3 ")
    await coach.coach_comment_written(written, state)
    assert "Держи темп &lt;3" in written.answer.await_args.args[0]
    # До «Отправить» ничего не записано.
    assert await fresh_db.list_coach_comments(wid) == []

    bot = _bot()
    with i18n.use_lang("ru"):  # язык тренера в контексте — атлету это не важно
        sent = await _tap(COACH, "coach:cms", bot=bot)
    comments = await fresh_db.list_coach_comments(wid)
    assert [c["text"] for c in comments] == ["Держи темп <3"]
    assert "Отправил" in _screen(sent)

    chat_id, body = bot.send_message.await_args.args[:2]
    assert chat_id == ATHLETE and expected in body and "Держи темп &lt;3" in body
    assert "Иван Петров" in body
    if athlete_lang == "en":
        assert not i18n_coverage.has_cyrillic(body.replace("Иван Петров", "").replace("Держи темп", ""))
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == f"hist:item:{wid}"

    # Второй тап по «Отправить» не шлёт тот же комментарий ещё раз.
    again = await _tap(COACH, "coach:cms", bot=bot)
    assert "Черновик потерялся" in _alert(again)
    assert len(await fresh_db.list_coach_comments(wid)) == 1


async def test_athlete_sees_coach_comments_under_the_workout_in_history(fresh_db):
    await _linked(fresh_db)
    wid = await _finished_workout(fresh_db, ATHLETE)
    await fresh_db.add_coach_comment(COACH, ATHLETE, wid, "Отлично, держи темп")
    callback = _callback(ATHLETE, f"hist:item:{wid}")
    assert await history.show_history_item(callback, wid)
    text = _screen(callback)
    # Тренер один — по имени в заголовке, чтобы блок не путали с 🤖 AI-тренером (аудит u7).
    assert "Иван Петров, твой тренер" in text and "Отлично, держи темп" in text
    # Открыл — прочитал; тренер видит отметку.
    assert (await fresh_db.list_coach_comments(wid))[0]["read_at"] is not None
    seen = await _tap(COACH, f"coach:w:{ATHLETE}:{wid}")
    assert "атлет прочитал" in _screen(seen)


async def test_two_coaches_under_one_workout_are_named_each(fresh_db):
    """Дневник открыт двоим — заголовок «Твои тренеры» и имя у каждого
    комментария; одно имя в заголовке приписало бы оба комментария одному."""
    await _linked(fresh_db)
    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Ольга")
    assert await fresh_db.create_coach_link(STRANGER, ATHLETE, "Маша", show_bodyweight=False)
    wid = await _finished_workout(fresh_db, ATHLETE)
    await fresh_db.add_coach_comment(COACH, ATHLETE, wid, "Держи темп")
    await fresh_db.add_coach_comment(STRANGER, ATHLETE, wid, "Добавь подход")
    callback = _callback(ATHLETE, f"hist:item:{wid}")
    assert await history.show_history_item(callback, wid)
    text = _screen(callback)
    assert "Твои тренеры" in text and "твой тренер" not in text
    assert "<b>Иван Петров</b>" in text and "<b>Ольга</b>" in text


async def test_telegram_delivery_skips_app_only_athletes(fresh_db):
    await _linked(fresh_db)
    wid = await _finished_workout(fresh_db, ATHLETE)
    bot = _bot()
    client = dict(await fresh_db.get_user(ATHLETE))
    client["telegram_linked"] = 0
    assert await coach_notify.send_telegram_comment(bot, client, "Иван", "ок", wid) is False
    bot.send_message.assert_not_awaited()
    # Остановил бота — комментарий не падает, просто не доставлен.
    bot.send_message = AsyncMock(side_effect=RuntimeError("Forbidden: bot was blocked"))
    assert await coach_notify.send_telegram_comment(bot, await fresh_db.get_user(ATHLETE), "Иван", "ок", wid) is False


async def test_rest_comment_also_goes_to_telegram(fresh_db, monkeypatch):
    """Комментарий из приложения доезжает и в Telegram — тем же coach_notify."""
    await _linked(fresh_db)
    wid = await _finished_workout(fresh_db, ATHLETE)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(coach_notify, "_send_with_own_bot", sent)
    import httpx

    import api_v1

    token = await fresh_db.issue_api_token(COACH)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")
    resp = await client.post(
        f"/coach/clients/{ATHLETE}/workouts/{wid}/comments", json={"text": "Ок"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    args = sent.await_args.args
    assert args[0]["telegram_id"] == ATHLETE and args[2] == "Ок" and args[3] == wid


# ---------- назначить программу ----------


async def test_assign_program_and_coach_label_in_programs(fresh_db):
    await _linked(fresh_db)
    gid = await fresh_db.create_muscle_group(COACH, "Спина")
    ex = await fresh_db.create_exercise(COACH, "Тяга штанги", gid)
    program_id = await fresh_db.create_program(COACH, "Сплит")
    day = await fresh_db.create_routine(COACH, "День А", program_id=program_id)
    await fresh_db.add_routine_exercise(day, ex, 0, "3×8")
    empty_id = await fresh_db.create_program(COACH, "Пусто")

    pick = await _tap(COACH, f"coach:asg:{ATHLETE}")
    cbs = [cb for _t, cb in _buttons(_kb(pick))]
    assert f"coach:asp:{ATHLETE}:{program_id}" in cbs

    done = await _tap(COACH, f"coach:asp:{ATHLETE}:{program_id}")
    assert "Назначил «Сплит»" in _screen(done)
    assert "сделано 0 из 1" in _screen(done)
    empty = await _tap(COACH, f"coach:asp:{ATHLETE}:{empty_id}")
    assert "нет упражнений" in _alert(empty)

    # У атлета программа подписана «Тренер: Иван Петров» — в списке и на экране.
    from handlers import routines

    copy = next(p for p in await fresh_db.list_programs(ATHLETE) if p["name"] == "Сплит")
    listing = _callback(ATHLETE, "rt:manage")
    await routines.show_manage(listing, _state(ATHLETE))
    labels = [t for t, _cb in _buttons(listing.message.answer.await_args.kwargs["reply_markup"])]
    assert any("Тренер: Иван Петров" in label for label in labels)
    screen = _callback(ATHLETE, f"rt:prg:{copy['id']}")
    await routines._show_program(screen, _state(ATHLETE), copy["id"])
    assert "Тренер: Иван Петров" in _screen(screen)
    with i18n.use_lang("en"):
        await routines._show_program(screen, _state(ATHLETE), copy["id"])
    assert "Coach: Иван Петров" in _screen(screen)




async def test_assign_without_programs_offers_catalog_and_ai_not_a_dead_end(fresh_db):
    """Пустой экран назначения раньше был тупиком — только текст «собери во
    вкладке программ». Теперь под ним готовые программы и AI-тренер."""
    await _linked(fresh_db)
    pick = await _tap(COACH, f"coach:asg:{ATHLETE}")
    cbs = [cb for _t, cb in _buttons(_kb(pick))]
    assert cbs[:2] == [f"coach:asc:{ATHLETE}", f"coach:asai:{ATHLETE}"]
    assert cbs[-1] == f"coach:cl:{ATHLETE}"
    assert "заведи прямо отсюда" in _screen(pick)


async def test_catalog_from_assign_adds_to_coach_and_returns_with_it_on_top(fresh_db):
    await _linked(fresh_db)
    await _coach_program(fresh_db, "Своя")
    catalog = await _tap(COACH, f"coach:asc:{ATHLETE}")
    assert "ГОТОВЫЕ ПРОГРАММЫ" in _screen(catalog)
    cbs = [cb for _t, cb in _buttons(_kb(catalog))]
    assert f"coach:ascp:{ATHLETE}:ppl" in cbs
    assert cbs[-1] == f"coach:asg:{ATHLETE}"

    added = await _tap(COACH, f"coach:ascp:{ATHLETE}:ppl")
    mine = {p["name"]: p["id"] for p in await fresh_db.list_programs(COACH)}
    assert len(mine) == 2
    new_id = next(pid for name, pid in mine.items() if name != "Своя")
    cbs = [cb for _t, cb in _buttons(_kb(added))]
    # Только что взятая — первой, и экран снова назначение этому атлету.
    assert cbs[0] == f"coach:asp:{ATHLETE}:{new_id}"
    assert "Добавил «" in _screen(added)

    # Второй тап по той же — без второй копии: та же программа первой.
    again = await _tap(COACH, f"coach:ascp:{ATHLETE}:ppl")
    assert len(await fresh_db.list_programs(COACH)) == 2
    assert [cb for _t, cb in _buttons(_kb(again))][0] == f"coach:asp:{ATHLETE}:{new_id}"
    assert "уже есть" in _screen(again)

    # И назначается как любая своя.
    done = await _tap(COACH, f"coach:asp:{ATHLETE}:{new_id}")
    assert "Назначил «" in _screen(done)


async def test_catalog_from_assign_refuses_strangers(fresh_db):
    await _linked(fresh_db)
    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Чужой")
    tapped = await _tap(STRANGER, f"coach:ascp:{ATHLETE}:ppl")
    assert _alert(tapped)
    assert await fresh_db.list_programs(STRANGER) == []


async def test_ai_from_assign_refuses_a_stranger_before_opening_the_chat(fresh_db, monkeypatch):
    """«Составить с AI-тренером» чужим атлетом — алерт, чат не открывается и
    модель не зовётся (сам режим — tests/test_coach_program_chat.py)."""
    from handlers import ai_trainer as ai_chat

    await _linked(fresh_db)
    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Чужой")
    started = AsyncMock()
    monkeypatch.setattr(ai_chat, "start_client_program", started)
    tapped = await _tap(STRANGER, f"coach:asai:{ATHLETE}")
    assert _alert(tapped)
    started.assert_not_awaited()
    assert (await _state(STRANGER).get_data()).get("ai_client") is None


# ---------- тренерские события: программа атлету, новый клиент тренеру ----------


async def _coach_program(db, name: str = "Сплит") -> int:
    gid = await db.create_muscle_group(COACH, "Спина")
    ex = await db.create_exercise(COACH, "Тяга штанги", gid)
    program_id = await db.create_program(COACH, name)
    day = await db.create_routine(COACH, "День А", program_id=program_id)
    await db.add_routine_exercise(day, ex, 0, "3×8")
    return program_id


@pytest.mark.parametrize("athlete_lang, expected", [("ru", "ПРОГРАММА ОТ ТРЕНЕРА"), ("en", "A PROGRAM FROM YOUR COACH")])
async def test_assigned_program_reaches_the_athlete_in_their_language(fresh_db, monkeypatch, athlete_lang, expected):
    await _linked(fresh_db, athlete_lang=athlete_lang)
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    program_id = await _coach_program(fresh_db)
    bot = _bot()
    with i18n.use_lang("ru"):  # язык тренера в контексте — атлету это не важно
        await _tap(COACH, f"coach:asp:{ATHLETE}:{program_id}", bot=bot)
    copy = next(p for p in await fresh_db.list_programs(ATHLETE) if p["name"] == "Сплит")
    chat_id, body = bot.send_message.await_args.args[:2]
    assert chat_id == ATHLETE and expected in body
    assert "Иван Петров" in body and "Сплит" in body
    if athlete_lang == "en":
        assert not i18n_coverage.has_cyrillic(body.replace("Иван Петров", "").replace("Сплит", ""))
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        (i18n.t_in(athlete_lang, "btn.open_program"), f"rt:prg:{copy['id']}")
    ]


async def test_failed_assignment_sends_nothing(fresh_db):
    await _linked(fresh_db)
    empty_id = await fresh_db.create_program(COACH, "Пусто")
    bot = _bot()
    await _tap(COACH, f"coach:asp:{ATHLETE}:{empty_id}", bot=bot)
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("coach_lang, expected", [("ru", "НОВЫЙ КЛИЕНТ"), ("en", "NEW CLIENT")])
async def test_new_client_reaches_the_coach_in_their_language(fresh_db, monkeypatch, coach_lang, expected):
    code = await _coach_with_invite(fresh_db)
    await fresh_db.set_user_lang(COACH, coach_lang)
    await _user(fresh_db, ATHLETE, "ru")
    monkeypatch.setattr(apns, "is_configured", lambda: False)
    await _tap(ATHLETE, f"coach:ok:{code}:0")
    bot = _bot()
    with i18n.use_lang("ru"):  # язык атлета в контексте — тренеру это не важно
        await coach.coach_self_named(_message(ATHLETE, "Маша", bot=bot), _state(ATHLETE))
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is not None
    chat_id, body = bot.send_message.await_args.args[:2]
    assert chat_id == COACH and expected in body and "Маша" in body
    if coach_lang == "en":
        assert not i18n_coverage.has_cyrillic(body.replace("Маша", ""))
    kb = bot.send_message.await_args.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row] == [
        (i18n.t_in(coach_lang, "coach.btn.my_clients_short"), "coach:clients:0")
    ]


async def test_already_linked_does_not_notify_the_coach_again(fresh_db):
    code = await _linked(fresh_db)
    await _tap(ATHLETE, f"coach:ok:{code}:0")
    bot = _bot()
    await coach.coach_self_named(_message(ATHLETE, "Маша", bot=bot), _state(ATHLETE))
    bot.send_message.assert_not_awaited()


async def test_coach_event_banners_carry_routes(fresh_db, monkeypatch):
    """Банеры: на языке получателя, с маршрутом в программу и в «Мои клиенты»."""
    await _linked(fresh_db, athlete_lang="en")
    await fresh_db.register_push_token(ATHLETE, "ios", "tok-athlete")
    await fresh_db.register_push_token(COACH, "ios", "tok-coach")
    alerts: list[dict] = []

    async def send_alert(user_id, token, title, body, **kwargs):
        alerts.append({"user_id": user_id, "token": token, "title": title, "body": body, **kwargs})
        return True

    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "send_alert", send_alert)
    athlete = await fresh_db.get_user(ATHLETE)
    assert await coach_notify.push_ios_program(athlete, "Иван Петров", "Сплит", 77) == 1
    assert await coach_notify.push_ios_new_client(await fresh_db.get_user(COACH), "Маша") == 1
    program, client = alerts
    assert program["user_id"] == ATHLETE and program["title"] == "A program from your coach"
    assert "Иван Петров" in program["body"] and "Сплит" in program["body"]
    assert program["route"] == {"screen": "program", "program_id": 77}
    assert program["category"] == "coach_program"
    assert client["user_id"] == COACH and client["title"] == "Новый клиент" and "Маша" in client["body"]
    assert client["route"] == {"screen": "coach_clients"}
    assert client["category"] == "coach_new_client"


async def test_coach_event_delivery_never_raises(fresh_db, monkeypatch):
    """Программа уже у атлета, связка уже заведена — сбой доставки не должен
    стать ошибкой; аккаунту без Telegram в Telegram не пишем вовсе."""
    await _linked(fresh_db)
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(fresh_db, "get_push_tokens", AsyncMock(side_effect=RuntimeError("db is gone")))
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Forbidden: bot was blocked"))
    athlete = await fresh_db.get_user(ATHLETE)
    assert await coach_notify.deliver_program(athlete, "Иван", "Сплит", 1, bot=bot) == (0, False)
    assert await coach_notify.deliver_new_client(COACH, "Маша", bot=bot) == (0, False)
    assert await coach_notify.deliver_new_client(999_999, "Маша", bot=bot) == (0, False)

    # Без бота (REST) и без Telegram у получателя — короткоживущий бот даже
    # не заводится.
    import aiogram

    import config

    monkeypatch.setattr(config, "BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(aiogram, "Bot", MagicMock(side_effect=AssertionError("Bot for an app-only account")))
    app_only = await fresh_db.create_app_only_user(language_code="en")
    assert app_only["telegram_id"] < 0
    assert await coach_notify.deliver_program(app_only, "Иван", "Сплит", 1) == (0, False)
    assert await coach_notify.deliver_new_client(app_only["telegram_id"], "Маша") == (0, False)


async def test_rest_assign_and_accept_notify_through_coach_notify(fresh_db, monkeypatch):
    """REST /v1 зовёт те же доставки, что и бот, — без живого бота."""
    import httpx

    import api_v1

    code = await _coach_with_invite(fresh_db)
    await _user(fresh_db, ATHLETE)
    delivered_program = AsyncMock(return_value=(0, True))
    delivered_client = AsyncMock(return_value=(0, True))
    monkeypatch.setattr(coach_notify, "deliver_program", delivered_program)
    monkeypatch.setattr(coach_notify, "deliver_new_client", delivered_client)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    athlete_api = httpx.AsyncClient(transport=transport, base_url="http://test")
    athlete_api.headers["Authorization"] = f"Bearer {await fresh_db.issue_api_token(ATHLETE)}"
    resp = await athlete_api.post(f"/coach/invites/{code}/accept", json={"name": "Маша"})
    assert resp.status_code == 201, resp.text
    assert delivered_client.await_args.args == (COACH, "Маша")
    assert "bot" not in delivered_client.await_args.kwargs

    program_id = await _coach_program(fresh_db)
    coach_api = httpx.AsyncClient(transport=transport, base_url="http://test")
    coach_api.headers["Authorization"] = f"Bearer {await fresh_db.issue_api_token(COACH)}"
    resp = await coach_api.post(f"/coach/clients/{ATHLETE}/programs", json={"program_id": program_id})
    assert resp.status_code == 201, resp.text
    client, coach_name, name, new_id = delivered_program.await_args.args
    assert client["telegram_id"] == ATHLETE and coach_name == "Иван Петров"
    assert (name, new_id) == (resp.json()["name"], resp.json()["program_id"])

    # Отказ (пустая программа) — без доставки.
    delivered_program.reset_mock()
    empty_id = await fresh_db.create_program(COACH, "Пусто")
    resp = await coach_api.post(f"/coach/clients/{ATHLETE}/programs", json={"program_id": empty_id})
    assert resp.status_code == 400
    delivered_program.assert_not_awaited()
    await athlete_api.aclose()
    await coach_api.aclose()


# ---------- AI-сводка ----------


class _FakeCompletions:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                                prompt_tokens_details=None, completion_tokens_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))], usage=usage)


@pytest.fixture
def fake_model(monkeypatch):
    completions = _FakeCompletions("**Маше** пора в зал.")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: fake)
    return completions


async def test_ai_summary_all_and_one_with_stubbed_model(fresh_db, fake_model):
    await _linked(fresh_db)
    await _finished_workout(fresh_db, ATHLETE)

    # Атлет приложения (iOS-токен) не давал согласия на AI — модель не
    # зовётся, честная строка. Чисто телеграмный атлет попал бы в сводку и без
    # согласия (ai_trainer._background_ai_allowed).
    await fresh_db.register_push_token(ATHLETE, "ios", "a" * 64)
    nobody = await _tap(COACH, "coach:ai")
    assert "ни один клиент не разрешил" in _screen(nobody)
    assert fake_model.calls == []

    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    summary = await _tap(COACH, "coach:ai")
    text = _screen(summary)
    assert "СВОДКА ПО КЛИЕНТАМ" in text and "<b>Маше</b> пора в зал." in text
    assert "Маша" in fake_model.calls[-1]["messages"][1]["content"]
    assert await fresh_db.get_ai_question_count_today(COACH) == 1

    one = await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert "СВОДКА — Маша" in _screen(one)
    from handlers import ai_trainer as ai_chat

    assert COACH not in ai_chat._busy


async def test_saved_summary_opens_free_with_date_and_refresh_button(fresh_db, fake_model):
    """Оплаченная сводка лежит в базе: второй тап по «🤖 Сводка от AI» —
    бесплатно, с датой и кнопкой «Обновить сводку»; модель — только по ней."""
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _tap(COACH, "coach:ai")
    first = await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert "Сводка от" not in _screen(first)
    assert ("🤖 Обновить сводку", f"coach:ai_new:{ATHLETE}") not in _buttons(_kb(first))
    assert len(fake_model.calls) == 2

    again = await _tap(COACH, "coach:ai")
    text = _screen(again)
    assert "СВОДКА ПО КЛИЕНТАМ" in text and "<b>Маше</b> пора в зал." in text
    assert "Сводка от " in text and "Обновить сводку" in text
    assert ("🤖 Обновить сводку", "coach:ai_new") in _buttons(_kb(again))
    one = await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert "СВОДКА — Маша" in _screen(one)
    assert ("🤖 Обновить сводку", f"coach:ai_new:{ATHLETE}") in _buttons(_kb(one))
    assert len(fake_model.calls) == 2
    assert await fresh_db.get_ai_question_count_today(COACH) == 2

    fake_model.text = "Маша молодец."
    fresh = await _tap(COACH, f"coach:ai_new:{ATHLETE}")
    assert "Маша молодец." in _screen(fresh) and "Сводка от" not in _screen(fresh)
    assert len(fake_model.calls) == 3
    saved = await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert "Маша молодец." in _screen(saved)
    assert len(fake_model.calls) == 3

    # Сменил язык — русскую сводку не показываю, собираю заново.
    await fresh_db.set_user_lang(COACH, "en")
    fake_model.text = "Masha should train."
    with i18n.use_lang("en"):
        english = await _tap(COACH, "coach:ai")
    assert "CLIENT SUMMARY" in _screen(english) and "Masha should train." in _screen(english)
    assert len(fake_model.calls) == 4
    with i18n.use_lang("en"):
        english = await _tap(COACH, "coach:ai")
    assert "Summary from " in _screen(english) and len(fake_model.calls) == 4
    assert ("🤖 Refresh summary", "coach:ai_new") in _buttons(_kb(english))


async def test_removing_the_client_drops_his_summaries(fresh_db, fake_model):
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _tap(COACH, "coach:ai")
    await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert await fresh_db.get_coach_summary(COACH, None) is not None
    assert await fresh_db.get_coach_summary(COACH, ATHLETE) is not None
    await _tap(COACH, f"coach:rmy:{ATHLETE}")
    assert await fresh_db.get_coach_link(COACH, ATHLETE) is None
    assert await fresh_db.get_coach_summary(COACH, None) is None
    assert await fresh_db.get_coach_summary(COACH, ATHLETE) is None


async def test_ai_summary_limit_shows_the_limit_text(fresh_db, fake_model, monkeypatch):
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())

    async def blocked(user_id, kind):
        assert kind == ai_limits.KIND_QUESTION
        return ai_limits.Block(kind=kind, log="test", user_text="На сегодня вопросы кончились.")

    monkeypatch.setattr(ai_limits, "check", blocked)
    callback = await _tap(COACH, "coach:ai")
    assert callback.message.reply.await_args.args[0] == "На сегодня вопросы кончились."
    assert fake_model.calls == []


async def test_saved_summary_regenerates_when_clients_or_workouts_change(fresh_db, fake_model):
    """Сохранённая сводка — текущая, только пока данные те же (отпечаток в
    coach_summaries.fingerprint). Ничего не поменялось — бесплатно и с датой;
    новый клиент, новая тренировка, свежий рекорд — сводка собирается заново
    сама, без кнопки, а старая как текущая не показывается."""
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _finished_workout(fresh_db, ATHLETE, started_at=_days_ago(20))
    await _tap(COACH, "coach:ai")
    assert len(fake_model.calls) == 1

    # Ничего не поменялось — сохранённая, модель не зовётся.
    same = await _tap(COACH, "coach:ai")
    assert "Сводка от " in _screen(same) and "устарела" not in _screen(same)
    assert len(fake_model.calls) == 1

    # Новый клиент (Вика, без единой тренировки) — сводка про одну Машу уже
    # неправда: собираю заново, и Вика в данных есть.
    await _user(fresh_db, STRANGER)
    assert await fresh_db.create_coach_link(COACH, STRANGER, "Вика", show_bodyweight=False)
    fake_model.text = "Маша в зале, Вика ещё не начала."
    fresh = await _tap(COACH, "coach:ai")
    assert "Вика ещё не начала" in _screen(fresh)
    assert "Сводка от" not in _screen(fresh) and "устарела" not in _screen(fresh)
    assert len(fake_model.calls) == 2
    assert "Вика" in fake_model.calls[-1]["messages"][1]["content"]
    await _tap(COACH, "coach:ai")
    assert len(fake_model.calls) == 2

    # Новая тренировка — тоже.
    await _finished_workout(fresh_db, ATHLETE, started_at=_days_ago(1))
    await _tap(COACH, "coach:ai")
    assert len(fake_model.calls) == 3

    # Сводка по одному клиенту: свежий рекорд (подход правят вверх) — заново.
    await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert len(fake_model.calls) == 4
    await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert len(fake_model.calls) == 4
    cur = await fresh_db.conn().execute(
        "SELECT s.id FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
        "JOIN workouts w ON w.id = b.workout_id WHERE w.user_id = ? ORDER BY w.started_at DESC LIMIT 1",
        (ATHLETE,),
    )
    await fresh_db.update_set((await cur.fetchone())["id"], 100.0, 5)
    assert (await coach_service.recent_records(await fresh_db.get_user(ATHLETE)))[0]["best_set"] == "100×5"
    await _tap(COACH, f"coach:ai:{ATHLETE}")
    assert len(fake_model.calls) == 5
    assert "100×5" in fake_model.calls[-1]["messages"][1]["content"]


async def test_stale_summary_with_spent_limit_shows_old_one_marked_stale(fresh_db, fake_model, monkeypatch):
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _tap(COACH, "coach:ai")
    await _finished_workout(fresh_db, ATHLETE)

    async def blocked(user_id, kind):
        return ai_limits.Block(kind=kind, log="test", user_text="На сегодня вопросы кончились.")

    monkeypatch.setattr(ai_limits, "check", blocked)
    callback = await _tap(COACH, "coach:ai")
    assert callback.message.reply.await_args.args[0] == "На сегодня вопросы кончились."
    text = _screen(callback)
    assert "<b>Маше</b> пора в зал." in text
    assert "устарела: данные с тех пор поменялись" in text and "Нужна свежая" not in text
    assert ("🤖 Обновить сводку", "coach:ai_new") in _buttons(_kb(callback))
    assert len(fake_model.calls) == 1


async def test_summary_from_before_fingerprints_counts_as_stale(fresh_db, fake_model):
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _tap(COACH, "coach:ai")
    await fresh_db.conn().execute("UPDATE coach_summaries SET fingerprint = NULL")
    await fresh_db.conn().commit()
    with i18n.use_lang("ru"):
        assert (await coach_service.saved_summary(COACH))["stale"] is True
    await _tap(COACH, "coach:ai")
    assert len(fake_model.calls) == 2


async def test_fingerprint_follows_visibility_consent_and_set_edits(fresh_db):
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    wid = await _finished_workout(fresh_db, ATHLETE)
    with i18n.use_lang("ru"):
        base = await coach_service.summary_fingerprint(COACH)
        assert await coach_service.summary_fingerprint(COACH) == base
        await fresh_db.conn().execute("UPDATE coach_links SET show_bodyweight = 1")
        await fresh_db.conn().commit()
        visible = await coach_service.summary_fingerprint(COACH)
        assert visible != base
        sets = await fresh_db.list_sets_for_workout(wid)
        await fresh_db.update_set(sets[0]["id"], 82.5, 5)
        edited = await coach_service.summary_fingerprint(COACH)
        assert edited != visible
        # Атлет приложения без согласия на AI выпадает из сводки — тоже другой отпечаток.
        await fresh_db.update_user(ATHLETE, ai_consent_at=None)
        await fresh_db.register_push_token(ATHLETE, "ios", "a" * 64)
        assert await coach_service.summary_fingerprint(COACH) != edited


async def test_summary_data_carries_set_weights_in_set_format(fresh_db, fake_model):
    """Модель видит сами подходы — «62.5×9», а не только «Жим лёжа — подходов:
    3»: без весов ей нечего сравнивать, и сводка честно писала «весов нет».
    Рекорд — тем же видом; голос — «я», никаких «мы»."""
    await _linked(fresh_db)
    await fresh_db.update_user(ATHLETE, ai_consent_at=fresh_db.now_iso())
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(20))
    wid = await _finished_workout(fresh_db, ATHLETE, weight=62.5, started_at=_days_ago(1))
    sets = await fresh_db.list_sets_for_workout(wid)
    await fresh_db.update_set(sets[0]["id"], 62.5, 9)
    block = sets[0]["block_id"]
    await fresh_db.add_set(block, sets[0]["exercise_id"], 2, 0, 60.0, 8)
    await _tap(COACH, "coach:ai")
    system, data = (m["content"] for m in fake_model.calls[-1]["messages"])
    assert "Жим лёжа 62.5×9, 60×8" in data
    assert "(подходов: 2)" in data and "Жим лёжа 60×5" in data
    assert "Рекорд за неделю: Жим лёжа — 62.5×9" in data
    assert "вес в кг" in data
    assert "никаких «мы»" in system and "62.5×9" in system

    await fresh_db.set_user_lang(COACH, "en")
    with i18n.use_lang("en"):
        await _tap(COACH, f"coach:ai:{ATHLETE}")
    system, data = (m["content"] for m in fake_model.calls[-1]["messages"])
    assert "62.5×9, 60×8" in data and "(sets: 2)" in data and "weight in kg" in data
    assert 'never "we"' in system


# ---------- язык ----------


async def test_english_coach_screens_have_no_cyrillic_beyond_user_data(fresh_db):
    await _linked(fresh_db)
    await fresh_db.set_user_lang(COACH, "en")
    wid = await _finished_workout(fresh_db, ATHLETE)
    with i18n.use_lang("en"):
        screens = [
            await _tap(COACH, "coach:hub"),
            await _tap(COACH, "coach:invite"),
            await _tap(COACH, "coach:clients:0"),
            await _tap(COACH, f"coach:cl:{ATHLETE}"),
            await _tap(COACH, f"coach:w:{ATHLETE}:{wid}"),
            await _tap(COACH, f"coach:asg:{ATHLETE}"),
        ]
    for callback in screens:
        text = _screen(callback) + " " + " ".join(t for t, _ in _buttons(_kb(callback)))
        for data in ("Иван Петров", "Маша", "Жим лёжа", "Грудь"):
            text = text.replace(data, "")
        assert not i18n_coverage.has_cyrillic(text), text


async def test_invite_code_extraction():
    assert coach._extract_code(" k7mp-2qxa ") == "K7MP2QXA"
    assert coach._extract_code("https://t.me/Bot?start=coach_K7MP2QXA") == "K7MP2QXA"
    assert coach._extract_code("trainingdiary://coach/k7mp2qxa") == "K7MP2QXA"
    assert coach_service.normalize_invite_code("") == ""


async def test_main_menu_shows_my_clients_only_for_coach_with_clients():
    import keyboards

    def callbacks(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "coach:clients:0" not in callbacks(keyboards.main_menu(has_active_workout=False))
    markup = keyboards.main_menu(has_active_workout=False, coach_clients=3)
    rows = markup.inline_keyboard
    # отдельной строкой над всеми пунктами меню
    assert [b.callback_data for b in rows[0]] == ["coach:clients:0"]
    assert rows[0][0].text.endswith("3")
    assert rows[1][0].callback_data == "menu:start_workout"


async def test_main_menu_kb_counts_coach_clients(fresh_db):
    from handlers import workout

    kb = await workout._main_menu_kb(COACH, None)
    assert "coach:clients:0" not in [b.callback_data for row in kb.inline_keyboard for b in row]
    await fresh_db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=False)
    kb = await workout._main_menu_kb(COACH, None)
    assert "coach:clients:0" in [b.callback_data for row in kb.inline_keyboard for b in row]


# ---------- 📈 прогресс клиента ----------


async def _tap_photo(user_id: int, data: str):
    """Нажать кнопку, экран которой — картинка (график)."""
    callback = _callback(user_id, data)
    callback.message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=5))
    result = await coach.router.callback_query.trigger(callback, state=_state(user_id))
    assert result is not UNHANDLED, f"кнопку {data} не забрал никто"
    return callback


async def test_progress_button_screen_and_lift_chart(fresh_db):
    await _linked(fresh_db)
    # Первая — до окна свежих рекордов (28 дней): без прежнего максимума
    # рекорда нет.
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(40))
    await _finished_workout(fresh_db, ATHLETE, weight=62.5, started_at=_days_ago(8))
    await _finished_workout(fresh_db, ATHLETE, weight=70.0)

    card = await _tap(COACH, f"coach:cl:{ATHLETE}")
    assert ("📈 Прогресс", f"coach:pg:{ATHLETE}") in _buttons(_kb(card))

    screen = await _tap(COACH, f"coach:pg:{ATHLETE}")
    text = _screen(screen)
    assert "ПРОГРЕСС — Маша" in text
    assert "За 12 недель — 3 тренировки" in text and "в среднем 0.2 в неделю" in text
    assert "1 тренировка · 350кг" in text  # 70×5 на этой неделе
    assert "Свежие рекорды" in text and "Жим лёжа — 70×5" in text
    assert "Лучшие подходы за всё время" in text and "70×5 · e1RM 81.7кг" in text
    lift_buttons = [cb for _t, cb in _buttons(_kb(screen)) if cb.startswith("coach:pgl:")]
    assert len(lift_buttons) == 1

    chart = await _tap_photo(COACH, lift_buttons[0])
    kwargs = chart.message.answer_photo.await_args.kwargs
    assert "Жим лёжа" in kwargs["caption"] and "70×5 · e1RM 81.7кг" in kwargs["caption"]
    assert chart.message.answer_photo.await_args.args[0].data[:4] == b"\x89PNG"
    assert ("⬅️ Назад", f"coach:pg:{ATHLETE}") in _buttons(kwargs["reply_markup"])


async def test_progress_with_little_data_says_so(fresh_db):
    await _linked(fresh_db)
    empty = _screen(await _tap(COACH, f"coach:pg:{ATHLETE}"))
    assert "Пока мало тренировок для графика" in empty and "Лучшие подходы" not in empty

    wid = await _finished_workout(fresh_db, ATHLETE)
    one = await _tap(COACH, f"coach:pg:{ATHLETE}")
    assert "Пока мало тренировок для графика" in _screen(one)
    assert not [cb for _t, cb in _buttons(_kb(one)) if cb.startswith("coach:pgl:")]
    # Кнопка с упражнением, которого на графике нет, — честный отказ.
    cur = await fresh_db.conn().execute("SELECT exercise_id FROM sets s JOIN workout_blocks b ON b.id = s.block_id "
                                        "WHERE b.workout_id = ?", (wid,))
    ex = (await cur.fetchone())["exercise_id"]
    gone = await _tap(COACH, f"coach:pgl:{ATHLETE}:{ex}")
    assert "мало тренировок" in _alert(gone)


async def test_progress_refused_to_stranger_and_former_coach(fresh_db):
    await _linked(fresh_db)
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(8))
    await _finished_workout(fresh_db, ATHLETE)
    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Чужой")
    for data in (f"coach:pg:{ATHLETE}", f"coach:pgl:{ATHLETE}:1"):
        callback = await _tap(STRANGER, data)
        assert "не открыл тебе дневник" in _alert(callback), data
        callback.message.answer.assert_not_awaited()
    # Бывший тренер: атлет закрыл доступ — прогресса больше нет.
    await fresh_db.delete_coach_link(COACH, ATHLETE)
    for data in (f"coach:pg:{ATHLETE}", f"coach:pgl:{ATHLETE}:1"):
        callback = await _tap(COACH, data)
        assert "не открыл тебе дневник" in _alert(callback), data
        callback.message.answer.assert_not_awaited()


async def test_english_progress_screen_has_no_cyrillic_beyond_user_data(fresh_db):
    await _linked(fresh_db)
    await fresh_db.set_user_lang(COACH, "en")
    await _finished_workout(fresh_db, ATHLETE, weight=60.0, started_at=_days_ago(8))
    await _finished_workout(fresh_db, ATHLETE)
    with i18n.use_lang("en"):
        screen = await _tap(COACH, f"coach:pg:{ATHLETE}")
        lift = [cb for _t, cb in _buttons(_kb(screen)) if cb.startswith("coach:pgl:")][0]
        chart = await _tap_photo(COACH, lift)
    texts = [
        _screen(screen) + " " + " ".join(t for t, _ in _buttons(_kb(screen))),
        chart.message.answer_photo.await_args.kwargs["caption"],
    ]
    for text in texts:
        for data in ("Маша", "Жим лёжа"):
            text = text.replace(data, "")
        assert not i18n_coverage.has_cyrillic(text), text
    assert "Last 12 weeks: 2 workouts" in texts[0]


# ---------- 📈 любое упражнение клиента ----------


async def _exercise_id(db, user_id: int, name: str) -> int:
    cur = await db.conn().execute("SELECT id FROM exercises WHERE user_id = ? AND name = ?", (user_id, name))
    return (await cur.fetchone())["id"]


async def test_all_exercises_list_and_progress_is_the_athletes_own_screen(fresh_db):
    await _linked(fresh_db)
    await _finished_workout(fresh_db, ATHLETE, "Присед", 100.0, started_at=_days_ago(9))
    await _finished_workout(fresh_db, ATHLETE, "Присед", 105.0, started_at=_days_ago(2))
    await _finished_workout(fresh_db, ATHLETE, "Бицепс", 15.0)
    squat = await _exercise_id(fresh_db, ATHLETE, "Присед")

    progress = await _tap(COACH, f"coach:pg:{ATHLETE}")
    assert ("📈 Все упражнения", f"coach:px:{ATHLETE}:0") in _buttons(_kb(progress))

    listing = await _tap(COACH, f"coach:px:{ATHLETE}:0")
    assert "ВСЕ УПРАЖНЕНИЯ — Маша" in _screen(listing)
    buttons = _buttons(_kb(listing))
    assert buttons[0] == ("Присед · 2 тренировки", f"coach:pe:{ATHLETE}:{squat}:20:0")
    assert buttons[1][0] == "Бицепс · 1 тренировка"
    assert buttons[-1] == ("⬅️ Назад", f"coach:pg:{ATHLETE}")

    chart = await _tap_photo(COACH, f"coach:pe:{ATHLETE}:{squat}:20:0")
    coach_view = chart.message.answer_photo.await_args
    assert coach_view.args[0].data[:4] == b"\x89PNG"
    # Тот же экран, что атлет открывает у себя: «📈 Прогресс → Присед».
    own = _callback(ATHLETE, f"prog:ex:{squat}:all")
    own.message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=5))
    assert await history.router.callback_query.trigger(own, state=_state(ATHLETE)) is not UNHANDLED
    assert coach_view.kwargs["caption"] == own.message.answer_photo.await_args.kwargs["caption"]
    assert "105×5" in coach_view.kwargs["caption"]
    # Только периоды и «Назад» — ни карточки упражнения, ни правки.
    kb = _buttons(coach_view.kwargs["reply_markup"])
    assert [cb for _t, cb in kb] == [
        f"coach:pe:{ATHLETE}:{squat}:10:0", f"coach:pe:{ATHLETE}:{squat}:20:0",
        f"coach:pe:{ATHLETE}:{squat}:9999:0", f"coach:px:{ATHLETE}:0",
    ]
    assert kb[1][0].startswith("• ")
    period = await _tap_photo(COACH, f"coach:pe:{ATHLETE}:{squat}:9999:0")
    assert _buttons(period.message.answer_photo.await_args.kwargs["reply_markup"])[2][0].startswith("• ")


async def test_all_exercises_pages_and_single_session_without_chart(fresh_db):
    await _linked(fresh_db)
    for i in range(coach.EXERCISES_PAGE_SIZE + 2):
        await _finished_workout(fresh_db, ATHLETE, f"Упражнение {i}", 20.0 + i)
    first = _buttons(_kb(await _tap(COACH, f"coach:px:{ATHLETE}:0")))
    assert (coach.keyboards.PAGE_NEXT_TEXT(), f"coach:px:{ATHLETE}:1") in first
    second = _buttons(_kb(await _tap(COACH, f"coach:px:{ATHLETE}:1")))
    assert len([cb for _t, cb in second if cb.startswith("coach:pe:")]) == 2
    assert (coach.keyboards.PAGE_PREV_TEXT(), f"coach:px:{ATHLETE}:0") in second
    # Одна тренировка — без картинки, текстом, как у атлета.
    ex = await _exercise_id(fresh_db, ATHLETE, "Упражнение 0")
    one = await _tap(COACH, f"coach:pe:{ATHLETE}:{ex}:20:1")
    assert "Нарисую график после второй тренировки" in _screen(one)
    assert _buttons(_kb(one))[-1] == ("⬅️ Назад", f"coach:px:{ATHLETE}:1")


async def test_all_exercises_empty_and_refusals(fresh_db):
    await _linked(fresh_db)
    empty = await _tap(COACH, f"coach:px:{ATHLETE}:0")
    assert "ни одного законченного подхода" in _screen(empty)

    await _finished_workout(fresh_db, ATHLETE, "Жим", 60.0)
    ex = await _exercise_id(fresh_db, ATHLETE, "Жим")
    await _finished_workout(fresh_db, COACH, "Свой жим", 100.0)
    own = await _exercise_id(fresh_db, COACH, "Свой жим")
    # Упражнение не этого атлета — отказ, своё тренера не показываем.
    gone = await _tap(COACH, f"coach:pe:{ATHLETE}:{own}:20:0")
    assert "Не нашёл это упражнение" in _alert(gone)
    gone.message.answer.assert_not_awaited()

    await _user(fresh_db, STRANGER)
    await fresh_db.upsert_coach_profile(STRANGER, "Чужой")
    for user_id in (STRANGER, ATHLETE):
        for data in (f"coach:px:{ATHLETE}:0", f"coach:pe:{ATHLETE}:{ex}:20:0"):
            callback = await _tap(user_id, data)
            assert "не открыл тебе дневник" in _alert(callback), (user_id, data)
            callback.message.answer.assert_not_awaited()
    await fresh_db.delete_coach_link(COACH, ATHLETE)
    for data in (f"coach:px:{ATHLETE}:0", f"coach:pe:{ATHLETE}:{ex}:20:0"):
        callback = await _tap(COACH, data)
        assert "не открыл тебе дневник" in _alert(callback), data


async def test_english_coach_sees_catalog_exercise_in_english(fresh_db):
    await _linked(fresh_db)
    await fresh_db.set_user_lang(COACH, "en")
    await _finished_workout(fresh_db, ATHLETE, "Жим штанги лёжа", 60.0, started_at=_days_ago(8))
    await _finished_workout(fresh_db, ATHLETE, "Жим штанги лёжа", 70.0)
    ex = await _exercise_id(fresh_db, ATHLETE, "Жим штанги лёжа")
    with i18n.use_lang("en"):
        progress = await _tap(COACH, f"coach:pg:{ATHLETE}")
        listing = await _tap(COACH, f"coach:px:{ATHLETE}:0")
        chart = await _tap_photo(COACH, f"coach:pe:{ATHLETE}:{ex}:20:0")
    assert ("📈 All exercises", f"coach:px:{ATHLETE}:0") in _buttons(_kb(progress))
    assert _buttons(_kb(listing))[0][0] == "Barbell Bench Press · 2 workouts"
    kwargs = chart.message.answer_photo.await_args.kwargs
    texts = [
        _screen(listing).replace("Маша", "") + " ".join(t for t, _ in _buttons(_kb(listing))),
        kwargs["caption"] + " ".join(t for t, _ in _buttons(kwargs["reply_markup"])),
    ]
    assert "Barbell Bench Press" in kwargs["caption"]
    for text in texts:
        assert not i18n_coverage.has_cyrillic(text), text
