"""Опросник перед сборкой программы: тренер спрашивает по одному, бот крутит локально.

Ключевой инвариант всей фичи — между вопросами модель НЕ вызывается. Весь
опросник приезжает одним вызовом (ai_trainer.ask_setup_questions), бот
раскладывает его по одному сообщению, копит ответы и уходит к модели ровно один
раз — уже за программой. Значит: шаги опросника не стоят ни рубля, ни единицы
дневной квоты, а «печатает…» человек ждёт дважды за всю сборку, а не пять раз.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import ai_trainer as ai_trainer_module
import state_scaffold
from handlers import ai_trainer

# asyncio_mode=auto (pytest.ini) — маркер async-тестам не нужен.

QUESTIONS = [
    {"question": "Сколько дней в неделю готов тренироваться?", "choices": ["2 дня", "3 дня", "4 дня"]},
    {"question": "Сколько времени есть на одну тренировку?", "choices": ["45 минут", "час-полтора"]},
    {"question": "Что болит или чего делать нельзя?", "choices": []},
]

ROUND_TWO = [
    {"question": "Штанга в зале есть?", "choices": ["Есть", "Только гантели"]},
]


class _Chat:
    """Чат одного пользователя: всё, что бот в него отправил, лежит в `sent`."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.sent: list[SimpleNamespace] = []
        self.bot = AsyncMock()

    def _blank(self, text: str | None = None):
        msg = MagicMock()
        msg.text = text
        msg.message_id = 1000 + len(self.sent)
        msg.chat = SimpleNamespace(id=self.user_id)
        msg.from_user = SimpleNamespace(id=self.user_id, username="tester")
        msg.bot = self.bot
        msg.reply = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.delete = AsyncMock()
        msg.answer = AsyncMock(side_effect=self._record)
        return msg

    async def _record(self, text: str, **kwargs):
        msg = self._blank()
        self.sent.append(SimpleNamespace(text=text, kwargs=kwargs, message=msg))
        return msg

    def user_message(self, text: str):
        return self._blank(text)

    def callback(self, data: str, msg_id: int = 777):
        """msg_id по умолчанию — тот же, что кладёт _state_with_setup: в жизни тап
        прилетает от того самого сообщения с вопросом, и обработчик это сверяет."""
        cb = MagicMock()
        cb.data = data
        cb.from_user = SimpleNamespace(id=self.user_id, username="tester")
        cb.message = self._blank()
        cb.message.message_id = msg_id
        cb.bot = self.bot
        cb.answer = AsyncMock()
        return cb

    @property
    def questions_shown(self) -> list[str]:
        return [item.text for item in self.sent if "ВОПРОС" in item.text]


async def _make_state(user_id: int) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state("AITrainerFlow:chatting")
    return state


async def _state_with_setup(
    user_id: int,
    questions=QUESTIONS,
    *,
    idx: int = 0,
    answers=(),
    rounds: int = 1,
    goal: str = "Хочу собрать программу тренировок",
    scenario: str | None = None,
) -> FSMContext:
    state = await _make_state(user_id)
    await state.update_data(
        ai_setup={
            "questions": [dict(q) for q in questions],
            "answers": list(answers),
            "idx": idx,
            "goal": goal,
            "rounds": rounds,
            "msg_id": 777,
            "scenario": scenario,
        }
    )
    return state


def _ask_recording(calls: list, questions=None, answer: str = "Понял, что уже знаю из истории."):
    """Подмена ai_trainer.ask: считает вызовы и при желании отдаёт опросник."""

    async def fake_ask(user_id, question, history, **kwargs):
        calls.append({"user_id": user_id, "question": question, "history": history})
        on_questions = kwargs.get("on_questions")
        if questions is not None and on_questions is not None:
            await on_questions([dict(q) for q in questions])
        return answer

    return fake_ask


# ---------- инструмент ask_setup_questions ----------

async def _ask_tool(user_id: int, tool_input: dict) -> tuple[dict, list | None]:
    captured: list[list] = []

    async def on_questions(questions: list) -> None:
        captured.append(questions)

    raw = await ai_trainer_module.execute_tool(
        user_id, "ask_setup_questions", tool_input, on_questions=on_questions
    )
    return json.loads(raw), (captured[-1] if captured else None)


async def test_tool_passes_questions_and_choices_through(fresh_db, user_id):
    payload, questions = await _ask_tool(user_id, {"questions": [dict(q) for q in QUESTIONS]})

    assert payload["asked"] is True
    assert questions is not None
    assert [q["question"] for q in questions] == [q["question"] for q in QUESTIONS]
    assert questions[0]["choices"] == QUESTIONS[0]["choices"]
    assert questions[2]["choices"] == []
    # Модель должна знать, что вопросы уже ушли и повторять их в тексте не надо.
    assert "по одному" in payload["note"]


async def test_tool_clamps_questions_and_choices_and_reports_it(fresh_db, user_id):
    payload, questions = await _ask_tool(
        user_id,
        {
            "questions": [
                {"question": f"Вопрос {i}", "choices": [f"в{j}" for j in range(6)]}
                for i in range(7)
            ]
        },
    )

    assert len(questions) == ai_trainer_module.SETUP_MAX_QUESTIONS
    assert all(len(q["choices"]) <= ai_trainer_module.SETUP_MAX_CHOICES for q in questions)
    assert "truncated_questions" in payload
    assert "truncated_choices" in payload


async def test_tool_shortens_long_question_and_choice(fresh_db, user_id):
    payload, questions = await _ask_tool(
        user_id,
        {"questions": [{"question": "я " * 200, "choices": ["вариант " * 20]}]},
    )

    assert len(questions[0]["question"]) <= ai_trainer_module.SETUP_QUESTION_LIMIT
    assert len(questions[0]["choices"][0]) <= ai_trainer_module.SETUP_CHOICE_LIMIT
    assert payload["clamped"]


async def test_tool_reports_empty_questionnaire_instead_of_asking(fresh_db, user_id):
    payload, questions = await _ask_tool(user_id, {"questions": [{"question": "   "}]})

    assert questions is None
    assert payload["asked"] is False
    assert "error" in payload


# ---------- показ: по одному вопросу на сообщение ----------

async def test_questionnaire_is_shown_one_question_per_message(fresh_db, user_id, monkeypatch):
    """Стена из четырёх пронумерованных вопросов одним сообщением — то, ради чего
    всё и затевалось: на экране должен быть ровно первый вопрос."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([], questions=QUESTIONS))
    state = await _make_state(user_id)
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("составь мне программу"), state)

    assert len(chat.questions_shown) == 1
    shown = chat.questions_shown[0]
    assert "ВОПРОС 1 ИЗ 3" in shown
    assert QUESTIONS[0]["question"] in shown
    assert QUESTIONS[1]["question"] not in "".join(item.text for item in chat.sent)

    setup = (await state.get_data())["ai_setup"]
    assert setup["idx"] == 0
    assert setup["answers"] == []
    assert setup["goal"] == "составь мне программу"


async def test_question_message_carries_choices_and_skip_button(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([], questions=QUESTIONS))
    state = await _make_state(user_id)
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("составь мне программу"), state)

    kb = [item for item in chat.sent if "ВОПРОС" in item.text][0].kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == ["ai:qa:0:0", "ai:qa:0:1", "ai:qa:0:2", "ai:qskip"]


# ---------- ответ кнопкой ----------

async def test_choice_records_answer_and_advances(fresh_db, user_id, monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id)
    chat = _Chat(user_id)

    await ai_trainer.ai_setup_choice(chat.callback("ai:qa:0:1"), state)

    setup = (await state.get_data())["ai_setup"]
    assert setup["answers"] == ["3 дня"]
    assert setup["idx"] == 1
    assert "ВОПРОС 2 ИЗ 3" in chat.questions_shown[-1]
    assert calls == []


async def test_answered_question_loses_its_buttons_and_shows_the_choice(fresh_db, user_id, monkeypatch):
    """Кнопки в чате живут вечно — под отвеченным вопросом их быть не должно."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([]))
    state = await _state_with_setup(user_id)
    chat = _Chat(user_id)

    await ai_trainer.ai_setup_choice(chat.callback("ai:qa:0:1"), state)

    kwargs = chat.bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 777
    assert kwargs["reply_markup"] is None
    assert "3 дня" in kwargs["text"]


async def test_tap_on_stale_question_changes_nothing(fresh_db, user_id, monkeypatch):
    """Проскроллил вверх, тапнул под прошлым вопросом — ответ не должен уехать
    в текущий."""
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id, idx=1, answers=["3 дня"])
    chat = _Chat(user_id)
    callback = chat.callback("ai:qa:0:0")

    await ai_trainer.ai_setup_choice(callback, state)

    setup = (await state.get_data())["ai_setup"]
    assert setup["idx"] == 1
    assert setup["answers"] == ["3 дня"]
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    assert calls == []
    assert chat.questions_shown == []


# ---------- ответ текстом ----------

async def test_text_answer_records_without_calling_model_or_quota(fresh_db, user_id, monkeypatch):
    """Встречный вопрос вместо ответа — тоже просто ответ: локальных детекторов
    «это вопрос» у нас нет, разбирается с ним финальная сборка."""
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id)
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("а сколько вообще надо"), state)

    assert calls == []
    assert await fresh_db.get_ai_question_count_today(user_id) == 0
    setup = (await state.get_data())["ai_setup"]
    assert setup["answers"] == ["а сколько вообще надо"]
    assert setup["idx"] == 1
    assert "ВОПРОС 2 ИЗ 3" in chat.questions_shown[-1]


# ---------- финал ----------

async def test_last_answer_calls_model_once_with_every_answer(fresh_db, user_id, monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", "час-полтора"])
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("болит правое колено"), state)

    assert len(calls) == 1
    asked = calls[0]["question"]
    for question in QUESTIONS:
        assert question["question"] in asked
    assert "3 дня" in asked
    assert "час-полтора" in asked
    assert "болит правое колено" in asked
    assert "Хочу собрать программу тренировок" in asked
    # Опросник погашен — следующая реплика человека уедет вопросом тренеру.
    assert ai_trainer._active_setup(await state.get_data()) is None


# ---------- анимированный progress_ui на сценарии «Составить программу» ----------


async def test_finish_setup_shows_progress_checklist_for_program_scenario(fresh_db, user_id, monkeypatch):
    """Финальный вызов _finish_setup — самый долгий во всём сценарии (см. его
    докстринг): помеченный scenario="program" опросник крутит progress_ui
    вместо голого "тренер думает", а по завершении заменяет экран ответом."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([]))
    state = await _state_with_setup(
        user_id, idx=2, answers=["3 дня", "час-полтора"], scenario="program"
    )
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("болит правое колено"), state)

    # Единственное отправленное сообщение — placeholder progress_ui, дальше
    # готовый ответ приходит через edit_text того же сообщения.
    assert len(chat.sent) == 1
    placeholder = chat.sent[0]
    assert placeholder.text == ai_trainer.progress_ui.initial_text(ai_trainer.PROGRAM_PROGRESS_STAGES)
    assert "0%" in placeholder.text
    assert "Посмотрел твою историю" in placeholder.text

    placeholder.message.edit_text.assert_awaited()
    edit_texts = [call.args[0] for call in placeholder.message.edit_text.await_args_list]
    # Ответ подошёл быстрее анимации (fake_ask ничего не ждёт) — прыжок сразу
    # на 100% со всеми галочками, а последним всегда идёт настоящий ответ.
    assert any("100%" in text for text in edit_texts)
    assert "Понял, что уже знаю из истории" in edit_texts[-1]


async def test_finish_setup_keeps_plain_placeholder_without_program_scenario(
    fresh_db, user_id, monkeypatch
):
    """Опросник без метки сценария (например, начатый вручную словами, а не
    кнопкой «Составить программу») не должен внезапно обрасти чек-листом —
    остаётся прежнее поведение (running_texts)."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([]))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", "час-полтора"], scenario=None)
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("болит правое колено"), state)

    assert len(chat.sent) == 1
    placeholder = chat.sent[0]
    assert "%" not in placeholder.text
    assert placeholder.text != ai_trainer.progress_ui.initial_text(ai_trainer.PROGRAM_PROGRESS_STAGES)


async def test_finish_setup_progress_screen_survives_provider_failure(fresh_db, user_id, monkeypatch):
    """Если сам вызов модели упал, анимация не должна зависнуть на моменте
    последнего тика — placeholder обязан смениться на честную ошибку."""

    async def boom(*args, **kwargs):
        raise RuntimeError("xai exploded")

    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", boom)
    state = await _state_with_setup(
        user_id, idx=2, answers=["3 дня", "час-полтора"], scenario="program"
    )
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("болит правое колено"), state)

    placeholder = chat.sent[0]
    placeholder.message.edit_text.assert_awaited_once()
    error_text = placeholder.message.edit_text.await_args.args[0]
    assert "не получилось" in error_text.lower()
    assert "%" not in error_text


async def test_answers_reach_history_in_human_readable_form(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([]))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", "час-полтора"])
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("колено"), state)

    history = (await state.get_data())["ai_history"]
    assert history[0]["role"] == "user"
    assert "Сколько дней в неделю готов тренироваться? — 3 дня" in history[0]["content"]


async def test_skip_midway_moves_to_the_next_question(fresh_db, user_id, monkeypatch):
    """Раньше «пропустить» обрывало опросник целиком, и пропустить один неудобный
    вопрос было нельзя — только все сразу. Теперь это ответ «пропустил» на
    текущий вопрос, а модель ждёт до последнего."""
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id, idx=1, answers=["3 дня"])
    chat = _Chat(user_id)

    await ai_trainer.ai_setup_skip(chat.callback("ai:qskip"), state)

    assert calls == [], "на середине модель не зовём"
    setup = ai_trainer._active_setup(await state.get_data())
    assert setup["idx"] == 2
    assert setup["answers"] == ["3 дня", None]
    assert chat.questions_shown, "следующий вопрос обязан показаться"


async def test_skipping_the_last_question_builds_on_partial_answers(fresh_db, user_id, monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", None])
    chat = _Chat(user_id)

    await ai_trainer.ai_setup_skip(chat.callback("ai:qskip"), state)

    assert len(calls) == 1
    asked = calls[0]["question"]
    assert "3 дня" in asked
    assert "пропустил" in asked
    assert "дефолт" in asked
    assert ai_trainer._active_setup(await state.get_data()) is None


# ---------- второй круг и потолок ----------

async def test_second_round_replaces_questionnaire_and_starts_over(fresh_db, user_id, monkeypatch):
    """Увидев «не знаю», тренер вправе переспросить — новый опросник заменяет
    старый и крутится с первого вопроса."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([], questions=ROUND_TWO))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", "час-полтора"])
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("не знаю"), state)

    setup = (await state.get_data())["ai_setup"]
    assert [q["question"] for q in setup["questions"]] == [q["question"] for q in ROUND_TWO]
    assert setup["idx"] == 0
    assert setup["answers"] == []
    assert setup["rounds"] == 2
    # Исходная цель переживает круги, а не подменяется простынёй с ответами.
    assert setup["goal"] == "Хочу собрать программу тренировок"
    assert "ВОПРОС 1 ИЗ 1" in chat.questions_shown[-1]


async def test_button_from_previous_round_does_not_answer_the_new_one(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([], questions=ROUND_TWO))
    state = await _state_with_setup(user_id, idx=2, answers=["3 дня", "час-полтора"])
    chat = _Chat(user_id)
    await ai_trainer.ai_question(chat.user_message("не знаю"), state)

    stale = chat.callback("ai:qa:1:0")
    await ai_trainer.ai_setup_choice(stale, state)

    setup = (await state.get_data())["ai_setup"]
    assert setup["answers"] == []
    assert setup["idx"] == 0
    assert stale.answer.await_args.kwargs.get("show_alert") is True


async def test_third_round_goes_straight_to_the_build_on_defaults(fresh_db, user_id, monkeypatch):
    """Иначе тренер способен гонять уточнения по кругу, и программы человек не
    увидит никогда."""
    calls: list = []
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording(calls, questions=ROUND_TWO))
    state = await _state_with_setup(
        user_id, ROUND_TWO, idx=0, rounds=ai_trainer.SETUP_MAX_ROUNDS
    )
    chat = _Chat(user_id)

    await ai_trainer.ai_question(chat.user_message("не знаю"), state)

    # Финальная сборка плюс принудительная — и ни одного нового вопроса на экране.
    assert len(calls) == 2
    assert "дефолт" in calls[1]["question"].lower()
    assert chat.questions_shown == []
    assert ai_trainer._active_setup(await state.get_data()) is None


# ---------- опросник переживает навигацию ----------

async def test_setup_survives_a_trip_to_the_menu(fresh_db, user_id):
    """Вопросы висят в чате живыми кнопками: заглянул в меню — вернулся на тот
    же вопрос, а не к ряду кнопок «этот вопрос уже позади»."""
    assert "ai_setup" in state_scaffold.AI_STATE_KEYS
    state = await _state_with_setup(user_id, idx=1, answers=["3 дня"])
    await state.update_data(open_exercises={"жим": 1})

    await state_scaffold.clear_state_keep_ai(state)

    setup = ai_trainer._active_setup(await state.get_data())
    assert setup is not None
    assert setup["idx"] == 1
    assert setup["answers"] == ["3 дня"]


async def test_setup_survives_workout_scaffold_clear(fresh_db, user_id):
    state = await _state_with_setup(user_id, idx=1, answers=["3 дня"])

    await state_scaffold.clear_state_keep_workout(state)

    assert ai_trainer._active_setup(await state.get_data()) is not None


def test_finishing_frames_demand_the_tool_call_not_just_a_retelling():
    """Регрессия: закрывающий опросник фрейм объяснял только, как НЕ переспрашивать,
    и модель послушно называла дефолты словами, останавливаясь на этом. Человек
    отвечал на четыре вопроса и не получал ни состава, ни кнопки сохранения."""
    for frame in (ai_trainer.SETUP_ANSWERS_FRAME, ai_trainer.SETUP_ENOUGH_FRAME):
        assert "propose_program" in frame


async def test_button_from_an_abandoned_questionnaire_does_not_answer_the_live_one(
    fresh_db, user_id, monkeypatch
):
    """Регрессия: индекса мало. Брошенный опросник оставляет свой вопрос с живыми
    кнопками, и если он встал на том же шаге, что текущий, тап по нему уезжал
    ответом в новый опросник — молча и не тем вариантом, что был под пальцем.
    Воспроизведено вживую: старый «ВОПРОС 3» ответил за текущий."""
    monkeypatch.setattr(ai_trainer.ai_trainer, "ask", _ask_recording([]))
    state = await _state_with_setup(user_id)
    chat = _Chat(user_id)

    # Тот же индекс вопроса, но сообщение — чужое, из прошлого опросника.
    cb = chat.callback("ai:qa:0:1", msg_id=555)
    await ai_trainer.ai_setup_choice(cb, state)

    setup = (await state.get_data())["ai_setup"]
    assert setup["answers"] == []
    assert setup["idx"] == 0
    cb.answer.assert_awaited()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
