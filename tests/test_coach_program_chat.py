"""Программа атлету в чате AI-тренера (coach_program_ai, режим «для атлета»).

Тренер собирает программу клиенту тем же чатом, что атлет себе: тот же
опросник, тот же propose_program, та же «Забрать». Здесь держится то, чем
режим отличается:

- инструменты читают дневник АТЛЕТА, только то, что тренер видит сам (без
  профиля, вес тела — только открытый), и ничего не пишут;
- чужой атлет или атлет без согласия на AI — отказ до модели;
- лимит и цена — тренера, программа ложится тренеру и назначается кнопкой;
- свой разговор тренера на время режима откладывается целиком и
  возвращается байт-в-байт (бот — FSM, приложение — отдельный разговор).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from luna_fakes import as_luna

import ai_limits
import ai_trainer
import coach_program_ai
import config
import db
import i18n
from handlers import ai_trainer as ai_chat

COACH = 701
ATHLETE = 702
STRANGER = 703


async def _user(telegram_id: int, lang: str = "ru"):
    await db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    await db.set_user_lang(telegram_id, lang)


async def _finished_workout(user_id: int, exercise: str, weight: float):
    gid = await db.create_muscle_group(user_id, "Спина")
    ex = await db.create_exercise(user_id, exercise, gid)
    wid = await db.create_workout(user_id)
    block = await db.create_block(wid, "single")
    await db.add_block_exercise(block, ex, 0)
    await db.add_set(block, ex, 1, 0, weight, 5)
    await db.finish_workout(wid)


async def _setup(*, bw: bool = False):
    """Тренер и атлет с РАЗНЫМИ упражнениями, профиль атлета с целью и травмой
    (тренер его не видит), вес тела атлета."""
    await _user(COACH)
    await db.upsert_coach_profile(COACH, "Иван Петров")
    await _user(ATHLETE)
    assert await db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=bw)
    await _finished_workout(COACH, "Тренерский жим узким", 100)
    await _finished_workout(ATHLETE, "Атлетская тяга блока", 42.5)
    await db.add_bodyweight_log(ATHLETE, 71.3)
    await db.conn().execute(
        "UPDATE users SET goal = ?, limitations = ? WHERE telegram_id = ?",
        ("Секретная цель атлета", "Больное колено атлета", ATHLETE),
    )
    await db.conn().commit()


def _call(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


PROGRAM = {
    "name": "Сплит для Маши",
    "description": "Три дня на всё тело.",
    "replaces_program": "Своя тренерская",
    "days": [{"name": "День А", "exercises": [
        {"name": "Жим штанги лёжа", "sets": 3, "reps_min": 5, "reps_max": 8},
    ]}],
}

FINAL = "Собрал три дня на всё тело: база в начале, прогрессия по повторам. Программа ждёт под сообщением."


def _scripted_model(monkeypatch, rounds: list) -> list[dict]:
    """Подмена одного раунда модели: что ей отдали и под чьим id, какие
    инструменты и какой слот кэша. Раунды — по списку, потом финальный текст."""
    seen: list[dict] = []

    async def fake_round(client, messages, user_id, on_chunk=None, include_tools=True, tools=None, cache_scope="trainer"):
        seen.append({
            "messages": [dict(m) for m in messages], "user_id": user_id,
            "tools": tools, "scope": cache_scope,
        })
        if rounds:
            return rounds.pop(0)
        return FINAL, [], ""

    monkeypatch.setattr(ai_trainer, "_completion_round", fake_round)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: as_luna(object()))
    return seen


# ---------- ядро: ai_trainer.ask с субъектом ----------


async def test_client_chat_reads_the_athletes_diary_not_the_coachs(fresh_db, monkeypatch):
    await _setup()
    seen = _scripted_model(monkeypatch, [
        ("", [_call("c1", "get_training_overview", {}), _call("c2", "list_recent_workouts", {"limit": 5})], ""),
        ("", [_call("c3", "propose_program", PROGRAM)], ""),
    ])
    drafts: list[dict] = []
    wire: dict = {}

    async def on_program(draft):
        drafts.append(draft)

    async def on_wire(messages):
        wire["messages"] = messages

    subject = await coach_program_ai.open_chat(COACH, ATHLETE)
    with i18n.use_lang("ru"):
        answer = await ai_trainer.ask(
            COACH, "собери Маше программу", [], subject=subject, on_program=on_program, on_wire=on_wire,
        )
    assert answer == FINAL

    # Платит и спрашивает тренер, слот кэша — свой у режима.
    assert {round_["user_id"] for round_ in seen} == {COACH}
    assert {round_["scope"] for round_ in seen} == {"coach_client"}
    # Инструменты — только чтение, опросник и propose_program.
    names = {t["function"]["name"] for t in seen[0]["tools"]}
    assert names == coach_program_ai.CLIENT_TOOL_NAMES
    for forbidden in ("save_athlete_profile", "log_bodyweight", "log_food", "get_food_diary",
                      "get_full_chat_history", "delete_program", "create_exercise", "get_saved_programs"):
        assert forbidden not in names
    # Заметка режима — вторым системным сообщением, с именем атлета.
    assert seen[0]["messages"][1]["role"] == "system" and "Маша" in seen[0]["messages"][1]["content"]

    tool_results = "\n".join(m["content"] for m in seen[1]["messages"] if m["role"] == "tool")
    assert "Атлетская тяга блока" in tool_results and "42.5" in tool_results
    assert "Тренерский жим узким" not in tool_results
    # Профиль атлета и закрытый вес тела тренер не видит — модель от его имени тоже.
    assert "Секретная цель" not in tool_results and "Больное колено" not in tool_results
    assert "71.3" not in tool_results

    # Черновик — тренеру новой программой, с адресатом и без правки его программ.
    assert len(drafts) == 1
    assert drafts[0]["for_client"] == {"client_id": ATHLETE, "name": "Маша"}
    assert drafts[0]["replaces"] is None
    # В историю шапка (системный промпт и заметка режима) не попадает.
    assert all(m["role"] != "system" for m in wire["messages"])
    # Дневник атлета не тронут.
    assert await db.list_programs(ATHLETE) == []


async def test_client_chat_sees_bodyweight_only_when_the_athlete_opened_it(fresh_db):
    await _setup()
    closed = await coach_program_ai.open_chat(COACH, ATHLETE)
    assert "error" in json.loads(await closed.execute("get_bodyweight_history", {}))
    assert "71.3" not in await closed.execute("get_training_overview", {})

    await db.update_coach_link_visibility(COACH, ATHLETE, show_bodyweight=True)
    opened = await coach_program_ai.open_chat(COACH, ATHLETE)
    assert "71.3" in await opened.execute("get_bodyweight_history", {})
    assert "71.3" in await opened.execute("get_training_overview", {})


async def test_client_chat_refuses_writes_even_if_the_model_asks(fresh_db):
    await _setup()
    subject = await coach_program_ai.open_chat(COACH, ATHLETE)
    out = json.loads(await subject.execute("log_bodyweight", {"weight": 90}))
    assert "error" in out
    out = json.loads(await subject.execute("save_athlete_profile", {"goal": "масса"}))
    assert "error" in out
    assert (await db.get_latest_bodyweight(ATHLETE))["weight"] == 71.3
    assert (await db.get_user(COACH))["goal"] in (None, "")


async def test_stranger_and_no_consent_are_refused_before_the_model(fresh_db, monkeypatch):
    await _setup()
    await _user(STRANGER)
    await db.upsert_coach_profile(STRANGER, "Чужой")
    with pytest.raises(coach_program_ai.ModeError) as exc:
        await coach_program_ai.open_chat(STRANGER, ATHLETE)
    assert exc.value.code == "not_linked"

    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    with pytest.raises(coach_program_ai.ModeError) as exc:
        await coach_program_ai.open_chat(COACH, ATHLETE)
    assert exc.value.code == "client_no_ai_consent"


# ---------- бот: вход, ход, выход, сохранение ----------


class _Chat:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.sent: list[SimpleNamespace] = []
        self.bot = AsyncMock()

    def _blank(self, text: str | None = None):
        msg = MagicMock()
        msg.text = text
        msg.message_id = 1000 + len(self.sent)
        msg.chat = SimpleNamespace(id=self.user_id)
        msg.from_user = SimpleNamespace(id=self.user_id, username="tester", language_code=None)
        msg.bot = self.bot
        msg.reply = AsyncMock(side_effect=self._record)
        msg.edit_text = AsyncMock(side_effect=self._record)
        msg.delete = AsyncMock()
        msg.answer = AsyncMock(side_effect=self._record)
        return msg

    async def _record(self, text: str, **kwargs):
        msg = self._blank()
        self.sent.append(SimpleNamespace(text=text, kwargs=kwargs, message=msg))
        return msg

    def user_message(self, text: str):
        return self._blank(text)

    def callback(self, data: str):
        cb = MagicMock()
        cb.data = data
        cb.from_user = SimpleNamespace(id=self.user_id, username="tester", language_code=None)
        cb.message = self._blank()
        cb.bot = self.bot
        cb.answer = AsyncMock()
        return cb


def _state(user_id: int, storage=None) -> FSMContext:
    return FSMContext(storage=storage or MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


OWN_HISTORY = [
    {"role": "user", "content": "мой вопрос про мой жим"},
    {"role": "assistant", "content": "мой ответ", "reasoning_content": "мысль"},
]


async def test_bot_entry_stashes_own_chat_and_exit_returns_it_byte_for_byte(fresh_db, monkeypatch):
    await _setup()
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    state = _state(COACH)
    await state.update_data(ai_history=OWN_HISTORY, ai_program_draft={"id": "own1", "name": "Моя", "days": [1]})
    asked: list = []
    monkeypatch.setattr(ai_chat, "_handle_question", AsyncMock(side_effect=lambda *a, **k: asked.append((a, k))))
    chat = _Chat(COACH)

    with i18n.use_lang("ru"):
        await ai_chat.start_client_program(chat.callback(f"coach:asai:{ATHLETE}"), state, ATHLETE, "Маша")

    data = await state.get_data()
    assert data["ai_client"] == {"client_id": ATHLETE, "name": "Маша"}
    assert data["ai_history"] is None and data["ai_program_draft"] is None
    assert data["ai_own_stash"]["ai_history"] == OWN_HISTORY
    # Тот же сценарий «Составить программу», только просьба — про атлета.
    (args, kwargs), = asked
    assert args[2] == i18n.t_in("ru", "coach.ai_chat.seed", name="Маша")
    assert kwargs["scenario"] == "program"

    with i18n.use_lang("ru"):
        await ai_chat.ai_client_exit(chat.callback("ai:client:exit"), state)
    data = await state.get_data()
    assert data["ai_client"] is None and data["ai_own_stash"] is None
    assert data["ai_history"] == OWN_HISTORY
    assert data["ai_program_draft"]["id"] == "own1"


async def test_bot_turn_passes_the_athlete_and_marks_the_answer(fresh_db, monkeypatch):
    await _setup()
    state = _state(COACH)
    await state.set_state("AITrainerFlow:chatting")
    await state.update_data(ai_client={"client_id": ATHLETE, "name": "Маша"}, ai_own_stash={})
    calls: list = []
    goal_question = i18n.t_in("ru", "coach.ai_chat.setup_goal")

    async def fake_ask(user_id, question, history, **kwargs):
        calls.append({"user_id": user_id, "subject": kwargs.get("subject")})
        await kwargs["on_questions"]([{"question": "Штанга в зале есть?", "choices": ["Есть", "Нет"]}])
        return "Посмотрел дневник атлета."

    checked: list = []
    real_check = ai_limits.check

    async def spy_check(user_id, kind):
        checked.append(user_id)
        return await real_check(user_id, kind)

    monkeypatch.setattr(ai_chat.ai_trainer, "ask", fake_ask)
    monkeypatch.setattr(ai_limits, "check", spy_check)
    chat = _Chat(COACH)
    with i18n.use_lang("ru"):
        await ai_chat.ai_question(chat.user_message("собирай"), state)

    assert calls[0]["user_id"] == COACH and calls[0]["subject"].client_id == ATHLETE
    assert COACH in checked and ATHLETE not in checked
    answer = next(item for item in chat.sent if "Посмотрел дневник" in item.text)
    assert "Составляем программу для атлета: Маша" in answer.text
    assert ("↩️ Выйти в свой чат", "ai:client:exit") in _buttons(answer.kwargs["reply_markup"])
    # Вопрос про цель — про атлета, и первым.
    setup = (await state.get_data())["ai_setup"]
    assert setup["questions"][0]["question"] == goal_question
    # В вечный лог переписки тренера разговор про атлета не пишется.
    assert list(await db.get_ai_chat_history(COACH)) == []
    # И в журнал для /aichats тоже: он под id тренера пережил бы доступ атлета.
    assert await db.count_ai_turn_log(COACH) == 0


async def test_bot_turn_refuses_when_the_athlete_closed_the_diary(fresh_db, monkeypatch):
    await _setup()
    state = _state(COACH)
    await state.set_state("AITrainerFlow:chatting")
    await state.update_data(ai_client={"client_id": ATHLETE, "name": "Маша"}, ai_own_stash={})
    await db.delete_coach_link(COACH, ATHLETE)
    ask = AsyncMock()
    monkeypatch.setattr(ai_chat.ai_trainer, "ask", ask)
    chat = _Chat(COACH)
    with i18n.use_lang("ru"):
        await ai_chat.ai_question(chat.user_message("собирай"), state)
    ask.assert_not_awaited()
    assert chat.sent[-1].text == i18n.t_in("ru", "api.error.not_linked")


async def test_bot_save_puts_it_to_the_coach_and_offers_assign(fresh_db, monkeypatch):
    from handlers import coach

    await _setup()
    await db.create_program(COACH, "Сплит для Маши")
    subject = await coach_program_ai.open_chat(COACH, ATHLETE)
    _payload, draft = await ai_trainer._propose_program(COACH, PROGRAM)
    draft.update(id="d1", replaces=None, for_client=subject.as_json())
    storage = MemoryStorage()
    state = _state(COACH, storage)
    await state.update_data(ai_program_draft=draft, ai_client={"client_id": ATHLETE, "name": "Маша"})
    chat = _Chat(COACH)
    cb = chat.callback("ai:prog:save:d1")
    with i18n.use_lang("ru"):
        await ai_chat.ai_program_save(cb, state)

    # Имя занято своей программой тренера — новая ложится рядом, без экрана конфликта.
    mine = {p["name"]: p["id"] for p in await db.list_programs(COACH)}
    assert "Сплит для Маши" in mine and len(mine) == 2
    new_id = max(mine.values())
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    first = markup.inline_keyboard[0][0]
    assert first.callback_data == f"coach:asp:{ATHLETE}:{new_id}"
    assert all(not (b.callback_data or "").startswith("rt:start:") for row in markup.inline_keyboard for b in row)
    assert await db.list_programs(ATHLETE) == []

    # «Назначить» — тот же обработчик, что на экране назначения.
    tap = chat.callback(first.callback_data)
    tap.message.answer = AsyncMock(return_value=SimpleNamespace(message_id=5))
    with i18n.use_lang("ru"):
        await coach.router.callback_query.trigger(tap, state=_state(COACH, storage))
    assert len(await db.list_programs(ATHLETE)) == 1


# ---------- /v1: отдельный разговор, свой — нетронут ----------


@pytest.fixture
async def api(fresh_db, monkeypatch):
    import httpx

    import api_v1

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    await _setup()
    token = await db.issue_api_token(COACH)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


async def test_v1_client_chat_is_a_separate_conversation(api, monkeypatch):
    own_wire = [{"role": "user", "content": "мой вопрос"}, {"role": "assistant", "content": "мой ответ"}]
    await db.add_ai_conversation_turn(COACH, "мой вопрос", "мой ответ", own_wire)
    own_id = await db.current_ai_conversation_id(COACH)
    seen: list = []

    async def fake_ask(user_id, question, history, **kwargs):
        seen.append({"user_id": user_id, "question": question, "history": history, "subject": kwargs.get("subject")})
        await kwargs["on_program"]({**(await ai_trainer._propose_program(COACH, PROGRAM))[1],
                                    "replaces": None, "for_client": {"client_id": ATHLETE, "name": "Маша"}})
        await kwargs["on_wire"]([*history, {"role": "user", "content": question}, {"role": "assistant", "content": FINAL}])
        return FINAL

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    resp = await api.post("/ai/conversations/coach-client", json={"client_id": ATHLETE})
    assert resp.status_code == 201
    assert resp.json()["subject"] == {"client_id": ATHLETE, "name": "Маша"}
    history = (await api.get("/ai/history")).json()
    assert history["messages"] == [] and history["subject"]["name"] == "Маша"

    seed = i18n.t_in("ru", "ai.screen.build_program_seed")
    resp = await api.post("/ai/ask", json={"question": seed, "shown_question": "🗂 Составь мне программу"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Атлет с тренировкой — не чистый лист: ход модели, история пустая (своя тренера не попала).
    assert seen[0]["history"] == [] and seen[0]["subject"].client_id == ATHLETE
    assert seen[0]["question"] == i18n.t_in("ru", "coach.ai_chat.seed", name="Маша")
    assert body["program"]["for_client"] == {"client_id": ATHLETE, "name": "Маша"}
    assert body["program"]["can_train_now"] is False
    assert body["mentions"] == {"exercises": [], "programs": []}
    shown = (await api.get("/ai/history")).json()["messages"][0]["text"]
    assert shown == i18n.t_in("ru", "coach.ai_chat.shown_seed", name="Маша")

    saved = await api.post("/ai/program/save", json={"draft_id": body["program"]["draft_id"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["for_client"] == {"client_id": ATHLETE, "name": "Маша"}
    program_id = saved.json()["program_id"]
    assert program_id in {p["id"] for p in await db.list_programs(COACH)}
    assert await db.list_programs(ATHLETE) == []
    assigned = await api.post(f"/coach/clients/{ATHLETE}/programs", json={"program_id": program_id})
    assert assigned.status_code == 201

    # Выйти — свой разговор тренера с нетронутым wire.
    resp = await api.post("/ai/conversations/coach-client/exit")
    assert resp.json()["conversation_id"] == own_id
    assert await db.get_ai_conversation_wire_history(COACH) == own_wire
    assert (await api.get("/ai/history")).json()["subject"] is None


async def test_v1_client_chat_refusals_do_not_reach_the_model(api, monkeypatch):
    ask = AsyncMock()
    monkeypatch.setattr(ai_trainer, "ask", ask)
    assert (await api.post("/ai/conversations/coach-client", json={"client_id": STRANGER})).status_code == 404

    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    resp = await api.post("/ai/conversations/coach-client", json={"client_id": ATHLETE})
    assert resp.status_code == 409 and resp.json()["error"] == "client_no_ai_consent"
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", False)

    assert (await api.post("/ai/conversations/coach-client", json={"client_id": ATHLETE})).status_code == 201
    await db.delete_coach_link(COACH, ATHLETE)
    # Связка разорвана посреди разговора — режим снят вместе с его ходами.
    assert (await api.get("/ai/history")).json()["subject"] is None

    await db.create_coach_link(COACH, ATHLETE, "Маша", show_bodyweight=False)
    assert (await api.post("/ai/conversations/coach-client", json={"client_id": ATHLETE})).status_code == 201
    monkeypatch.setattr(config, "AI_CONSENT_REQUIRED", True)
    # Сам тренер согласие дал — отказ именно из-за атлета.
    await db.conn().execute("UPDATE users SET ai_consent_at = ? WHERE telegram_id = ?", (db.now_iso(), COACH))
    await db.conn().commit()
    resp = await api.post("/ai/ask", json={"question": "собирай"})
    assert resp.status_code == 409 and resp.json()["error"] == "client_no_ai_consent"
    ask.assert_not_awaited()


async def test_athlete_account_deletion_takes_the_coach_chat_about_them(api):
    assert (await api.post("/ai/conversations/coach-client", json={"client_id": ATHLETE})).status_code == 201
    await db.add_ai_conversation_turn(COACH, "про Машу", "ответ с её тягой 42.5", [])
    await db.wipe_user_account(ATHLETE)
    assert await db.get_ai_conversation_history(COACH) == []
    assert await db.get_coach_client_chat(COACH) is None


def test_is_seed_needs_the_whole_seed_not_just_its_opening():
    """Seed режима узнаётся целиком, с любым именем внутри. У турецкого начало
    seed — «Sporcum » («мой атлет»), и обычная реплика тренера с этого слова
    не должна подменяться просьбой собрать программу."""
    for lang in i18n.SUPPORTED:
        assert coach_program_ai.is_seed(i18n.t_in(lang, "coach.ai_chat.seed", name="Ayşe"))
        assert coach_program_ai.is_seed(i18n.t_in(lang, "ai.screen.build_program_seed"))
    assert not coach_program_ai.is_seed("Sporcum dizinden sakatlandı, squat yerine leg press koy")
    assert not coach_program_ai.is_seed("Build a training program for my athlete Bob, but keep squats")
