"""REST `/v1` для AI-тренера: дневная квота вопросов (`/ai/limits`) и сам
вопрос-ответ (`/ai/ask`).

Гоняется тем же приёмом, что и tests/test_api_v1.py и tests/test_api_v1_food.py
— httpx поверх ASGI, без сокета, включая настоящую проверку Bearer-токена.
Модель не вызывается по-настоящему нигде: `ai_trainer.ask` подменяется целиком
(как analyze_food в test_api_v1_food.py), поскольку она и есть публичная точка
входа `/ai/ask` — мокировать нужно именно её, а не HTTP-клиент x.ai глубже.
"""

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config

# Из глобального каталога (seed_data.EXERCISE_TEMPLATES) — резолвится у любого
# пользователя, даже пустого, тем же путём, что и в tests/test_ai_program_builder.py.
TEMPLATE_A = "Жим штанги лёжа"
TEMPLATE_B = "Присед со штангой"


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------- GET /ai/limits ----------


@pytest.mark.asyncio
async def test_limits_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/ai/limits")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_limits_fresh_user_has_full_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"]["used"] == 0
    assert body["question"]["limit"] == config.AI_QUESTION_DAILY_LIMIT
    assert body["question"]["remaining"] == config.AI_QUESTION_DAILY_LIMIT
    assert body["blocked"] is False
    assert body["block_reason"] is None
    assert body["configured"] is True


@pytest.mark.asyncio
async def test_limits_reports_exhausted_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.get("/ai/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"]["used"] == 1
    assert body["question"]["remaining"] == 0
    assert body["blocked"] is True
    assert body["block_reason"] == ai_limits.KIND_QUESTION


@pytest.mark.asyncio
async def test_limits_zero_config_means_unlimited(fresh_db, client_factory, monkeypatch):
    """limit <= 0 в конфиге значит «лимита нет» (см. ai_limits._exhausted) —
    отдаём тем же значением клиенту, а не отдельным флагом unlimited."""
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 0)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/limits")
    body = resp.json()
    assert body["question"]["limit"] is None
    assert body["question"]["remaining"] is None
    assert body["blocked"] is False


# ---------- POST /ai/ask ----------


@pytest.mark.asyncio
async def test_ask_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/ask", json={"question": "как дела?"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ask_requires_configuration(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: False)
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "как дела?"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_ask_rejects_empty_question(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_ask_rejects_missing_question_field(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ask_rejects_too_long_question(fresh_db, client_factory, monkeypatch):
    import api_v1_ai

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    too_long = "a" * (api_v1_ai.MAX_QUESTION_LENGTH + 1)
    resp = await client.post("/ai/ask", json={"question": too_long})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_ask_returns_model_answer_and_charges_quota(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        assert question == "Как мой прогресс?"
        # У нового пользователя персистентная история ещё пуста.
        assert history == []
        # on_program/on_action/on_questions/on_wire подключены (см. _run_turn в
        # api_v1_ai.py) — черновик программы, действия и опросник теперь
        # доезжают до ответа /ai/ask; on_chunk (стрим бота) остаётся
        # телеграм-специфичным и не подключён.
        assert set(kwargs) == {"on_program", "on_action", "on_questions", "on_wire"}
        for cb in kwargs.values():
            assert callable(cb)
        return "Ты молодец, продолжай в том же духе!"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Ты молодец, продолжай в том же духе!"
    assert body["limits"]["question"]["used"] == 1
    assert body["limits"]["question"]["remaining"] == config.AI_QUESTION_DAILY_LIMIT - 1

    # Квота реально списана в БД, а не только в ответе.
    assert await fresh_db.get_ai_question_count_today(111) == 1


@pytest.mark.asyncio
async def test_ask_does_not_charge_quota_on_provider_failure(fresh_db, client_factory, monkeypatch):
    """Сорвавшийся у провайдера запрос не должен стоить человеку вопроса —
    тот же приём, что в handlers/ai_trainer.py и api_v1_food.parse_food."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def boom(user_id, question, history, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ai_trainer, "ask", boom)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "Как мой прогресс?"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "answer_failed"
    assert await fresh_db.get_ai_question_count_today(111) == 0


@pytest.mark.asyncio
async def test_ask_respects_daily_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    # 0 отключает лимит вовсе (limit > 0 в ai_limits._exhausted) — нужен именно
    # уже выбранный лимит 1, а не выключенный.
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 1)
    ai_limits.reset_cache()

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.try_increment_ai_question_count(111, 1)

    resp = await client.post("/ai/ask", json={"question": "ещё один вопрос"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "question_limit_exceeded"


@pytest.mark.asyncio
async def test_ask_times_out_without_hanging(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_TOTAL_ANSWER_SECONDS", 0.01)

    async def hangs_forever(user_id, question, history, **kwargs):
        import asyncio

        await asyncio.sleep(10)
        return "никогда не дойдёт"

    monkeypatch.setattr(ai_trainer, "ask", hangs_forever)

    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/ai/ask", json={"question": "долгий вопрос"})
    assert resp.status_code == 504
    assert resp.json()["error"] == "timeout"
    # Таймаут — не ответ, счётчик не движется.
    assert await fresh_db.get_ai_question_count_today(111) == 0


# ---------- персистентная история диалога (ai_conversation_turns) ----------


def _fake_ask_with_wire():
    """fake ai_trainer.ask, ведущий себя как настоящий: дописывает вопрос и
    ответ к переданной history и отдаёт получившийся wire через on_wire —
    ровно так, как это делает _ask_plain в ai_trainer.py."""

    async def fake_ask(user_id, question, history, on_wire=None, **kwargs):
        answer = f"ответ на: {question}"
        if on_wire is not None:
            await on_wire(
                history
                + [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer},
                ]
            )
        return answer

    return fake_ask


@pytest.mark.asyncio
async def test_history_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/ai/history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_history_delete_requires_auth(client_factory):
    client = client_factory()
    resp = await client.delete("/ai/history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_history_empty_for_new_user(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/history")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


@pytest.mark.asyncio
async def test_history_has_two_turns_in_order_after_two_questions(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    client = await _linked_client(fresh_db, client_factory)

    resp1 = await client.post("/ai/ask", json={"question": "первый вопрос"})
    resp2 = await client.post("/ai/ask", json={"question": "второй вопрос"})
    assert resp1.status_code == 200 and resp2.status_code == 200

    resp = await client.get("/ai/history")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    # Роль/текст ровно как их нужно отрисовать чатом — без tool-calls и
    # прочего wire-формата (см. докстринг api_v1_ai.get_history).
    assert [(m["role"], m["text"]) for m in messages] == [
        ("user", "первый вопрос"),
        ("assistant", "ответ на: первый вопрос"),
        ("user", "второй вопрос"),
        ("assistant", "ответ на: второй вопрос"),
    ]
    assert all("created_at" in m for m in messages)


@pytest.mark.asyncio
async def test_ask_second_call_receives_nonempty_history(fresh_db, client_factory, monkeypatch):
    """Второй вопрос должен получить в ask(..., history=...) то, что уехало
    первым ходом — иначе персистентность истории не даёт памяти диалога."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    seen_histories: list[list] = []

    async def fake_ask(user_id, question, history, on_wire=None, **kwargs):
        seen_histories.append(history)
        answer = f"ответ на: {question}"
        if on_wire is not None:
            await on_wire(
                history
                + [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer},
                ]
            )
        return answer

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "первый вопрос"})
    await client.post("/ai/ask", json={"question": "второй вопрос"})

    assert seen_histories[0] == []
    assert seen_histories[1] != []
    assert {"role": "user", "content": "первый вопрос"} in seen_histories[1]
    assert {"role": "assistant", "content": "ответ на: первый вопрос"} in seen_histories[1]


@pytest.mark.asyncio
async def test_history_trims_to_max_turns(fresh_db, client_factory, monkeypatch):
    """Держим только последние MAX_AI_CONVERSATION_TURNS ходов — иначе
    бесконечный диалог означает бесконечно растущий промпт (см. db.py)."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    total = fresh_db.MAX_AI_CONVERSATION_TURNS + 5
    for i in range(total):
        await fresh_db.add_ai_conversation_turn(
            111, f"q{i}", f"a{i}", [{"role": "user", "content": f"q{i}"}]
        )

    resp = await client.get("/ai/history")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert len(messages) == fresh_db.MAX_AI_CONVERSATION_TURNS * 2
    # Срезаны самые старые, остались последние по порядку.
    kept_first_question = f"q{total - fresh_db.MAX_AI_CONVERSATION_TURNS}"
    assert messages[0]["text"] == kept_first_question
    assert messages[-1]["text"] == f"a{total - 1}"

    # Персистентный wire для следующего вопроса тоже берётся из невытесненного
    # хода — там реально хранится не больше MAX_AI_CONVERSATION_TURNS строк.
    rows = await fresh_db.get_ai_conversation_history(111, limit=1000)
    assert len(rows) == fresh_db.MAX_AI_CONVERSATION_TURNS


@pytest.mark.asyncio
async def test_history_delete_clears_it(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "вопрос"})
    resp = await client.get("/ai/history")
    assert resp.json()["messages"] != []

    resp = await client.delete("/ai/history")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is True

    resp = await client.get("/ai/history")
    assert resp.json()["messages"] == []

    # И wire для следующего вопроса снова пуст — «начать разговор заново»
    # должно очищать именно то, что подаётся в ask(..., history=...).
    assert await fresh_db.get_ai_conversation_wire_history(111) == []


@pytest.mark.asyncio
async def test_history_is_private_per_user(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())

    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    await client_a.post("/ai/ask", json={"question": "секретный вопрос A"})

    resp_a = await client_a.get("/ai/history")
    assert len(resp_a.json()["messages"]) == 2

    resp_b = await client_b.get("/ai/history")
    assert resp_b.json()["messages"] == []


# ---------- черновик программы (POST /ai/ask → program, POST /ai/program/save) ----------


def _fake_ask_proposing_program(name: str = "Фуллбоди"):
    """Как настоящий ask(): реально резолвит упражнения через propose_program
    (execute_tool), а не выдумывает форму черновика руками."""

    async def fake_ask(user_id, question, history, on_program=None, **kwargs):
        tool_input = {
            "name": name,
            "days": [
                {"name": "День 1", "exercises": [
                    {"name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 8},
                    {"name": TEMPLATE_B, "sets": 4, "reps_min": 6, "reps_max": 10},
                ]},
            ],
            "description": "Простая база на всё тело.",
        }
        await ai_trainer.execute_tool(user_id, "propose_program", tool_input, on_program=on_program)
        return "Собрал программу — жми кнопку под ответом."

    return fake_ask


@pytest.mark.asyncio
async def test_ask_returns_program_draft_with_composition(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "Собери мне программу"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    program = body["program"]
    assert program is not None
    assert program["name"] == "Фуллбоди"
    assert program["description"] == "Простая база на всё тело."
    # Один день — это тренировка, а не программа: по ней можно пойти прямо
    # сейчас, ничего себе не заводя (см. keyboards.ai_program_preview_keyboard).
    assert program["can_train_now"] is True
    assert program["replacing"] is False
    assert program["label"]  # локализованная подпись кнопки "Забрать: ..."
    assert len(program["days"]) == 1
    day = program["days"][0]
    assert day["name"] == "День 1"
    exercise_names = {item["name"] for item in day["items"]}
    assert exercise_names == {TEMPLATE_A, TEMPLATE_B}
    # Опросника в этом же ходе быть не должно — программа и опросник
    # взаимоисключающи (см. api_v1_ai._turn_response).
    assert body["questions"] is None


@pytest.mark.asyncio
async def test_save_program_creates_routine_with_days_and_exercises(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    ask_resp = await client.post("/ai/ask", json={"question": "Собери мне программу"})
    draft_id = ask_resp.json()["program"]["draft_id"]

    resp = await client.post("/ai/program/save", json={"draft_id": draft_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["day_count"] == 1
    assert body["replacing"] is False

    routines = await fresh_db.list_routines(111)
    assert len(routines) == 1
    assert routines[0]["program_name"] == "Фуллбоди"
    exercises = await fresh_db.list_routine_exercises(routines[0]["id"])
    assert {ex["display_name"] for ex in exercises} == {TEMPLATE_A, TEMPLATE_B}


@pytest.mark.asyncio
async def test_save_program_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/program/save", json={"draft_id": "whatever"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_save_program_rejects_missing_draft(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/program/save", json={"draft_id": "does-not-exist"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "draft_not_found"


@pytest.mark.asyncio
async def test_save_program_does_not_save_someone_elses_draft(fresh_db, client_factory, monkeypatch):
    """Черновик пользователя A не виден и не сохраняем пользователем B —
    ai_program_drafts ключуется по telegram_id из его же токена."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())

    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    ask_resp = await client_a.post("/ai/ask", json={"question": "Собери мне программу"})
    draft_id = ask_resp.json()["program"]["draft_id"]

    resp = await client_b.post("/ai/program/save", json={"draft_id": draft_id})
    assert resp.status_code == 404

    # У пользователя A черновик тем временем остаётся на месте и сохраняется как обычно.
    resp_a = await client_a.post("/ai/program/save", json={"draft_id": draft_id})
    assert resp_a.status_code == 200, resp_a.text
    assert await fresh_db.list_routines(111) != []
    assert await fresh_db.list_routines(222) == []


@pytest.mark.asyncio
async def test_ask_returns_mentioned_exercises_and_programs(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    await fresh_db.get_or_create_user_exercise_by_name(111, TEMPLATE_A)

    async def fake_ask(user_id, question, history, **kwargs):
        return f"Продолжай делать {TEMPLATE_A} — это твоё упражнение."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"question": "Что мне делать?"})
    assert resp.status_code == 200, resp.text
    mentions = resp.json()["mentions"]
    assert any(ex["display_name"] == TEMPLATE_A for ex in mentions["exercises"])


# ---------- опросник перед сборкой программы (POST /ai/questions/answer) ----------


def _fake_ask_with_questions(*question_texts: str):
    async def fake_ask(user_id, question, history, on_questions=None, **kwargs):
        if on_questions is not None:
            await on_questions([{"question": text, "choices": []} for text in question_texts])
        return "Уточню пару вещей, прежде чем собрать план."

    return fake_ask


@pytest.mark.asyncio
async def test_ask_returns_setup_questions(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_questions("Сколько дней в неделю?"))
    client = await _linked_client(fresh_db, client_factory)
    # Профиль с уже заполненной целью — иначе первым вопросом всегда встаёт
    # вопрос о цели (ai_setup_flow.questions_with_goal), и тест зависел бы от
    # текста, которого сам не задавал.
    await fresh_db.update_user(111, goal="набор массы")

    resp = await client.post("/ai/ask", json={"question": "Собери программу"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["program"] is None
    questions = body["questions"]
    assert questions is not None
    assert questions["index"] == 0
    assert questions["total"] == 1
    assert questions["question"] == "Сколько дней в неделю?"
    assert questions["skip_label"]


@pytest.mark.asyncio
async def test_questions_answer_advances_and_then_finishes(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        ai_trainer, "ask", _fake_ask_with_questions("Сколько дней в неделю?", "Есть травмы?")
    )
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, goal="набор массы")

    ask_resp = await client.post("/ai/ask", json={"question": "Собери программу"})
    assert ask_resp.json()["questions"]["total"] == 2

    # Первый ответ — просто продвигает опросник дальше, модель ещё не зовётся.
    resp1 = await client.post("/ai/questions/answer", json={"question_index": 0, "answer": "3 дня"})
    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    assert body1["questions"]["index"] == 1
    assert body1["answer"] is None

    # Второй (последний) ответ уходит собирать план — model.ask() вызывается снова.
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program("Программа после опроса"))
    resp2 = await client.post("/ai/questions/answer", json={"question_index": 1, "answer": None})
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["questions"] is None
    assert body2["program"]["name"] == "Программа после опроса"


@pytest.mark.asyncio
async def test_questions_answer_rejects_wrong_index(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_questions("Сколько дней в неделю?"))
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, goal="набор массы")

    await client.post("/ai/ask", json={"question": "Собери программу"})
    resp = await client.post("/ai/questions/answer", json={"question_index": 5, "answer": "что угодно"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "setup_stale"


@pytest.mark.asyncio
async def test_questions_answer_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/questions/answer", json={"question_index": 0, "answer": "x"})
    assert resp.status_code == 401
