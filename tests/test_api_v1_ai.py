"""REST `/v1` для AI-тренера: дневная квота вопросов (`/ai/limits`) и сам
вопрос-ответ (`/ai/ask`).

Гоняется тем же приёмом, что и tests/test_api_v1.py и tests/test_api_v1_food.py
— httpx поверх ASGI, без сокета, включая настоящую проверку Bearer-токена.
Модель не вызывается по-настоящему нигде: `ai_trainer.ask` подменяется целиком
(как analyze_food в test_api_v1_food.py), поскольку она и есть публичная точка
входа `/ai/ask` — мокировать нужно именно её, а не HTTP-клиент x.ai глубже.
"""

import base64
import datetime as dt

import httpx
import pytest

import ai_limits
import ai_trainer
import api_v1
import config
import db
import running_texts
import video_analysis

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
    # И бронь `_busy` снята — иначе после первого же таймаута человек был бы
    # заблокирован до конца жизни процесса (см. api_v1_ai._claim_turn_or_429).
    import api_v1_ai

    assert 111 not in api_v1_ai._busy


# ---------- гонка двух параллельных запросов (двойной платёж) ----------


@pytest.mark.asyncio
async def test_concurrent_asks_pay_the_model_only_once(fresh_db, client_factory, monkeypatch):
    """Два одновременных POST /ai/ask одного человека (двойной тап, ретрай) —
    раньше оба проходили ai_limits.check до того, как первый успевал
    отметиться (инкремент идёт только после ответа модели), и оба уходили в
    модель. Настоящая гонка, не единичный вызов: обе корутины реально стартуют
    и обе доходят до `ai_trainer.ask` одновременно, если бы не busy-замок."""
    import asyncio

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    calls = 0
    release = asyncio.Event()

    async def slow_ask(user_id, question, history, **kwargs):
        nonlocal calls
        calls += 1
        await release.wait()
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", slow_ask)

    client = await _linked_client(fresh_db, client_factory)

    async def fire():
        return await client.post("/ai/ask", json={"question": "Как мой прогресс?"})

    first_task = asyncio.ensure_future(fire())
    # Дать первой корутине реально дойти до `ask` и повиснуть на release,
    # прежде чем стартует вторая — иначе обе могут оказаться ещё до захвата
    # замка и тест ничего не докажет про сам захват.
    await asyncio.sleep(0.05)
    second_task = asyncio.ensure_future(fire())
    await asyncio.sleep(0.05)
    release.set()
    first_resp, second_resp = await asyncio.gather(first_task, second_task)

    statuses = sorted([first_resp.status_code, second_resp.status_code])
    assert statuses == [200, 429]
    busy_resp = first_resp if first_resp.status_code == 429 else second_resp
    assert busy_resp.json()["error"] == "busy"
    # Модель реально позвана ровно один раз — не два, как до защиты.
    assert calls == 1
    assert await fresh_db.get_ai_question_count_today(111) == 1

    import api_v1_ai

    assert 111 not in api_v1_ai._busy


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


# ---------- конфликт имени при сохранении (POST /ai/program/save, on_conflict) ----------


async def _ask_and_get_draft_id(client, name: str = "Фуллбоди") -> str:
    resp = await client.post("/ai/ask", json={"question": "Собери мне программу"})
    assert resp.status_code == 200, resp.text
    return resp.json()["program"]["draft_id"]


@pytest.mark.asyncio
async def test_save_program_without_on_conflict_returns_409_on_name_clash(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program("Фуллбоди"))
    client = await _linked_client(fresh_db, client_factory)

    draft_id = await _ask_and_get_draft_id(client)
    first = await client.post("/ai/program/save", json={"draft_id": draft_id})
    assert first.status_code == 200, first.text

    # Второе предложение под тем же именем — та же программа модели не
    # называет replaces_program, поэтому это чистый конфликт имени.
    draft_id_2 = await _ask_and_get_draft_id(client)
    resp = await client.post("/ai/program/save", json={"draft_id": draft_id_2})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "name_conflict"

    # Черновик остаётся на месте — можно попробовать ещё раз с on_conflict.
    routines = await fresh_db.list_routines(111)
    assert len(routines) == 1
    retry = await client.post("/ai/program/save", json={"draft_id": draft_id_2, "on_conflict": "copy"})
    assert retry.status_code == 200, retry.text


@pytest.mark.asyncio
async def test_save_program_on_conflict_replace_replaces_existing_program(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program("Фуллбоди"))
    client = await _linked_client(fresh_db, client_factory)

    draft_id = await _ask_and_get_draft_id(client)
    first = await client.post("/ai/program/save", json={"draft_id": draft_id})
    assert first.status_code == 200, first.text
    old_program_id = first.json()["program_id"]

    draft_id_2 = await _ask_and_get_draft_id(client)
    conflict = await client.post("/ai/program/save", json={"draft_id": draft_id_2})
    assert conflict.status_code == 409

    resp = await client.post(
        "/ai/program/save", json={"draft_id": draft_id_2, "on_conflict": "replace"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replacing"] is True
    assert body["program_id"] == old_program_id

    # Ровно одна программа с этим именем осталась — правка на месте, а не
    # вторая копия.
    programs = await fresh_db.list_programs(111)
    assert len([p for p in programs if p["name"] == "Фуллбоди"]) == 1


@pytest.mark.asyncio
async def test_save_program_on_conflict_copy_creates_second_program(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program("Фуллбоди"))
    client = await _linked_client(fresh_db, client_factory)

    draft_id = await _ask_and_get_draft_id(client)
    first = await client.post("/ai/program/save", json={"draft_id": draft_id})
    assert first.status_code == 200, first.text
    old_program_id = first.json()["program_id"]

    draft_id_2 = await _ask_and_get_draft_id(client)
    conflict = await client.post("/ai/program/save", json={"draft_id": draft_id_2})
    assert conflict.status_code == 409

    resp = await client.post("/ai/program/save", json={"draft_id": draft_id_2, "on_conflict": "copy"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replacing"] is False
    assert body["program_id"] != old_program_id

    programs = await fresh_db.list_programs(111)
    names = {p["name"] for p in programs}
    assert "Фуллбоди" in names
    assert len(names) == 2  # исходное имя + свободное под копию


@pytest.mark.asyncio
async def test_save_program_rejects_invalid_on_conflict(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    draft_id = await _ask_and_get_draft_id(client)
    resp = await client.post(
        "/ai/program/save", json={"draft_id": draft_id, "on_conflict": "delete"}
    )
    assert resp.status_code == 400


# ---------- «начать тренировку» по несохранённому черновику (POST /ai/program/train) ----------


@pytest.mark.asyncio
async def test_train_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/program/train", json={"draft_id": "whatever"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_train_rejects_missing_draft(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/program/train", json={"draft_id": "does-not-exist"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "draft_not_found"


@pytest.mark.asyncio
async def test_train_starts_workout_from_single_day_draft(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    draft_id = await _ask_and_get_draft_id(client)
    resp = await client.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["workout"]["status"] == "active"
    assert body["plan"]["name"] == "День 1"
    exercise_names = {item["display_name"] for item in body["plan"]["items"]}
    assert exercise_names == {TEMPLATE_A, TEMPLATE_B}

    workout = await fresh_db.get_active_workout(111)
    assert workout is not None
    assert workout["id"] == body["workout"]["id"]

    # Черновик израсходован — по нему уже начали заниматься.
    pending = await client.get("/ai/pending")
    assert pending.json()["program"] is None


def _fake_ask_proposing_multi_day_program(name: str = "Сплит"):
    async def fake_ask(user_id, question, history, on_program=None, **kwargs):
        tool_input = {
            "name": name,
            "days": [
                {"name": "День 1", "exercises": [{"name": TEMPLATE_A, "sets": 3, "reps_min": 5, "reps_max": 8}]},
                {"name": "День 2", "exercises": [{"name": TEMPLATE_B, "sets": 4, "reps_min": 6, "reps_max": 10}]},
            ],
            "description": None,
        }
        await ai_trainer.execute_tool(user_id, "propose_program", tool_input, on_program=on_program)
        return "Собрал программу — жми кнопку под ответом."

    return fake_ask


@pytest.mark.asyncio
async def test_train_rejects_multi_day_draft(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_multi_day_program())
    client = await _linked_client(fresh_db, client_factory)

    ask_resp = await client.post("/ai/ask", json={"question": "Собери мне сплит"})
    program = ask_resp.json()["program"]
    assert program["can_train_now"] is False
    draft_id = program["draft_id"]

    resp = await client.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp.status_code == 400

    # Черновик остаётся — многодневный отказ ничего не расходует.
    pending = await client.get("/ai/pending")
    assert pending.json()["program"] is not None


@pytest.mark.asyncio
async def test_train_reuses_existing_active_workout_and_skips_done_exercises(
    fresh_db, client_factory, monkeypatch
):
    """Поведение при уже открытой тренировке — как у обычного POST
    /workouts/active: она не заменяется, а до-планируется (см.
    ai_program_train в боте)."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    start_resp = await client.post("/workouts/active", json={})
    assert start_resp.status_code == 201, start_resp.text
    active_workout_id = start_resp.json()["id"]

    # TEMPLATE_A уже отработан в этой тренировке до захода на план.
    ex_a_id = await fresh_db.get_or_create_user_exercise_by_name(111, TEMPLATE_A)
    block_id = await fresh_db.get_or_create_single_block_for_exercise(active_workout_id, ex_a_id)
    await fresh_db.add_set(block_id, ex_a_id, 1, 1, weight=60, reps=5)

    draft_id = await _ask_and_get_draft_id(client)
    resp = await client.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp.status_code == 200, resp.text  # тренировка уже была — не создана заново
    body = resp.json()
    assert body["workout"]["id"] == active_workout_id

    # TEMPLATE_A уже сделан — в план добора попадает только TEMPLATE_B.
    exercise_names = {item["display_name"] for item in body["plan"]["items"]}
    assert exercise_names == {TEMPLATE_B}


@pytest.mark.asyncio
async def test_train_all_exercises_already_done_returns_409_and_keeps_draft(
    fresh_db, client_factory, monkeypatch
):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    start_resp = await client.post("/workouts/active", json={})
    active_workout_id = start_resp.json()["id"]
    for name in (TEMPLATE_A, TEMPLATE_B):
        ex_id = await fresh_db.get_or_create_user_exercise_by_name(111, name)
        block_id = await fresh_db.get_or_create_single_block_for_exercise(active_workout_id, ex_id)
        await fresh_db.add_set(block_id, ex_id, 1, 1, weight=60, reps=5)

    draft_id = await _ask_and_get_draft_id(client)
    resp = await client.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "all_done"

    pending = await client.get("/ai/pending")
    assert pending.json()["program"] is not None


@pytest.mark.asyncio
async def test_train_does_not_start_from_someone_elses_draft(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())

    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    draft_id = await _ask_and_get_draft_id(client_a)

    resp = await client_b.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp.status_code == 404
    assert await fresh_db.get_active_workout(222) is None

    resp_a = await client_a.post("/ai/program/train", json={"draft_id": draft_id})
    assert resp_a.status_code == 201, resp_a.text
    assert await fresh_db.get_active_workout(111) is not None


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


# ---------- незавершённое состояние разговора (GET /ai/pending) ----------


@pytest.mark.asyncio
async def test_pending_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/ai/pending")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pending_empty_when_nothing_hangs(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/pending")
    assert resp.status_code == 200
    assert resp.json() == {"program": None, "questions": None, "actions": []}


@pytest.mark.asyncio
async def test_pending_returns_program_draft_after_reload(fresh_db, client_factory, monkeypatch):
    """Тот самый сценарий из жалобы: тренер собрал программу, экран
    перезагрузили (новый GET, как будто вернулись в чат) — карточка должна
    восстановиться с настоящим draft_id, которым реально можно вызвать
    POST /ai/program/save."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())
    client = await _linked_client(fresh_db, client_factory)

    ask_resp = await client.post("/ai/ask", json={"question": "Собери мне программу"})
    draft_id = ask_resp.json()["program"]["draft_id"]

    resp = await client.get("/ai/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["questions"] is None
    assert body["program"]["draft_id"] == draft_id
    assert body["program"]["name"] == "Фуллбоди"
    assert body["program"]["label"]

    # И этим draft_id реально можно забрать программу — не бутафорский id.
    save_resp = await client.post("/ai/program/save", json={"draft_id": body["program"]["draft_id"]})
    assert save_resp.status_code == 200, save_resp.text


@pytest.mark.asyncio
async def test_pending_returns_current_setup_question_after_reload(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        ai_trainer, "ask", _fake_ask_with_questions("Сколько дней в неделю?", "Есть травмы?")
    )
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.update_user(111, goal="набор массы")

    await client.post("/ai/ask", json={"question": "Собери программу"})
    await client.post("/ai/questions/answer", json={"question_index": 0, "answer": "3 дня"})

    resp = await client.get("/ai/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["program"] is None
    assert body["questions"]["index"] == 1
    assert body["questions"]["total"] == 2
    assert body["questions"]["question"] == "Есть травмы?"
    assert body["questions"]["skip_label"]


@pytest.mark.asyncio
async def test_pending_is_private_per_user(fresh_db, client_factory, monkeypatch):
    """Черновик и опросник пользователя A не видны пользователю B — та же
    гарантия, что и у /ai/program/save (ключуется по telegram_id из токена)."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_proposing_program())

    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    await client_a.post("/ai/ask", json={"question": "Собери мне программу"})

    resp_a = await client_a.get("/ai/pending")
    assert resp_a.json()["program"] is not None

    resp_b = await client_b.get("/ai/pending")
    assert resp_b.json() == {"program": None, "questions": None, "actions": []}


# ---------- фото к вопросу (POST /ai/ask, image_data_url) ----------


def _image_data_url(mime="image/jpeg", payload=b"not-really-a-jpeg-but-fine-its-mocked"):
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


@pytest.mark.asyncio
async def test_ask_with_photo_passes_image_to_model(fresh_db, client_factory, monkeypatch):
    """Тот же сценарий, что ai_photo_question в боте: фото уходит в ask()
    отдельным аргументом, а вопрос без подписи заменяется дефолтным."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    data_url = _image_data_url()

    async def fake_ask(user_id, question, history, **kwargs):
        assert question == "Посмотри на фото и прокомментируй."
        assert kwargs["image_data_url"] == data_url
        return "Это тренажёр Смита."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"image_data_url": data_url})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "Это тренажёр Смита."


@pytest.mark.asyncio
async def test_ask_with_photo_and_caption_uses_caption_as_question(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    data_url = _image_data_url()

    async def fake_ask(user_id, question, history, **kwargs):
        assert question == "Что это за тренажёр?"
        assert kwargs["image_data_url"] == data_url
        return "Тренажёр для разгибания ног."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/ask", json={"question": "Что это за тренажёр?", "image_data_url": data_url}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_ask_rejects_too_big_photo(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    from handlers import ai_trainer as ai_trainer_handlers

    monkeypatch.setattr(ai_trainer_handlers, "MAX_IMAGE_BYTES", 4)
    import api_v1_ai

    monkeypatch.setattr(api_v1_ai, "MAX_IMAGE_BYTES", 4)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"image_data_url": _image_data_url()})
    assert resp.status_code == 400
    assert resp.json()["error"] == "photo_too_big"


@pytest.mark.asyncio
async def test_ask_rejects_unsupported_photo_format(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/ask", json={"image_data_url": _image_data_url(mime="application/pdf")}
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


# ---------- фото сохраняется на диск и видно в GET /ai/history ----------
#
# chat_attachments.py: раньше и фото, и видео к вопросу уходили только в
# модель, а в истории оставался текстовый маркер. Эти тесты проверяют
# ровно то, чего не было — что фото переживает уход с экрана.


@pytest.fixture
def chat_media_dir(tmp_path, monkeypatch):
    path = tmp_path / "ai_chat"
    monkeypatch.setattr(config, "AI_CHAT_MEDIA_DIR", str(path))
    return path


@pytest.mark.asyncio
async def test_ask_with_photo_saves_it_and_history_links_to_it(
    fresh_db, client_factory, monkeypatch, chat_media_dir
):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    payload = b"\xff\xd8\xff-jpeg-ish-bytes-for-the-test"
    data_url = _image_data_url(payload=payload)

    async def fake_ask(user_id, question, history, **kwargs):
        return "Это тренажёр Смита."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"image_data_url": data_url})
    assert resp.status_code == 200, resp.text

    # Файл реально лёг на диск — не только имя в БД.
    saved = list(chat_media_dir.glob("u111_*.jpg"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == payload

    hist = await client.get("/ai/history")
    assert hist.status_code == 200
    messages = hist.json()["messages"]
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["image_url"].startswith("/ai/history/")
    assert user_message["image_url"].endswith("/image")

    turn_id = user_message["image_url"].split("/")[3]
    image_resp = await client.get(f"/ai/history/{turn_id}/image")
    assert image_resp.status_code == 200
    assert image_resp.content == payload
    assert image_resp.headers["content-type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_history_image_requires_auth(fresh_db, client_factory, monkeypatch, chat_media_dir):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/ai/ask", json={"image_data_url": _image_data_url()})

    hist = await client.get("/ai/history")
    turn_id = next(m for m in hist.json()["messages"] if m["role"] == "user")["image_url"].split("/")[3]

    anon = client_factory()
    resp = await anon.get(f"/ai/history/{turn_id}/image")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_history_image_not_visible_to_other_user(
    fresh_db, client_factory, monkeypatch, chat_media_dir
):
    """Чужой ход по угаданному id — 404, не чужая картинка."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    owner = await _linked_client(fresh_db, client_factory)
    await owner.post("/ai/ask", json={"image_data_url": _image_data_url()})
    hist = await owner.get("/ai/history")
    turn_id = next(m for m in hist.json()["messages"] if m["role"] == "user")["image_url"].split("/")[3]

    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    resp = await intruder.get(f"/ai/history/{turn_id}/image")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ask_photo_save_failure_does_not_block_the_answer(
    fresh_db, client_factory, monkeypatch, chat_media_dir
):
    """Диск недоступен/save упал — вопрос тренеру всё равно должен ответить,
    просто без картинки в истории. Само хранение — не то, ради чего люди
    сюда пишут."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        return "Это тренажёр Смита."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    import chat_attachments

    def boom(*args, **kwargs):
        raise OSError("disk is on fire")

    monkeypatch.setattr(chat_attachments, "save_photo", boom)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/ask", json={"image_data_url": _image_data_url()})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answer"] == "Это тренажёр Смита."

    hist = await client.get("/ai/history")
    user_message = next(m for m in hist.json()["messages"] if m["role"] == "user")
    assert "image_url" not in user_message


# ---------- POST /ai/video ----------


def _video_data_url(mime="video/mp4", payload=b"not-really-a-video-but-fine-its-mocked"):
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _fake_analysis(view=None):
    return {"view": view or {}, "exercise_confidence": "высокая", "checklist": []}


@pytest.mark.asyncio
async def test_ask_video_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ask_video_returns_503_when_not_available(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: False)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 503
    assert resp.json()["error"] == "not_configured"


@pytest.mark.asyncio
async def test_ask_video_full_scenario_with_own_exercise(fresh_db, client_factory, monkeypatch):
    """Полный сценарий: свой exercise_id → разбор → ответ тренера, обе квоты
    (video и question) списаны один раз."""
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        assert exercise_hint == "Присед со штангой"
        assert mime_type == "video/mp4"
        return _fake_analysis()

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)

    async def fake_ask(user_id, question, history, **kwargs):
        assert "video_context" in kwargs
        assert question == "Разбери технику: Присед со штангой."
        return "Спина ровная, колени не заваливаются — норм."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)
    created = await client.post("/exercises", json={"name": "Присед со штангой"})
    exercise_id = created.json()["id"]

    resp = await client.post(
        "/ai/video",
        json={"video_data_url": _video_data_url(), "exercise_id": exercise_id, "duration_seconds": 12},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Спина ровная, колени не заваливаются — норм."
    assert await fresh_db.get_ai_video_count_today(111) == 1
    assert await fresh_db.get_ai_question_count_today(111) == 1


@pytest.mark.asyncio
async def test_ask_video_resolves_exercise_from_caption(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/exercises", json={"name": "Румынская тяга со штангой"})

    async def fake_analyze(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        assert exercise_hint == "Румынская тяга со штангой"
        return _fake_analysis()

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    resp = await client.post(
        "/ai/video",
        json={"video_data_url": _video_data_url(), "caption": "румынская тяга"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_ask_video_saves_frame_and_history_links_to_it(
    fresh_db, client_factory, monkeypatch, chat_media_dir
):
    """Сам ролик НЕ хранится — только кадр-превью, тем же путём в истории,
    что и у фото-вопроса (см. chat_attachments.save_video_frame)."""
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    async def fake_analyze(*a, **k):
        return _fake_analysis()

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)

    import chat_attachments

    frame_bytes = b"\xff\xd8\xff-a-jpeg-frame"

    def fake_save_frame(user_id, video_bytes):
        assert video_bytes  # сырые байты ролика дошли, а не подпись/что-то ещё
        return chat_attachments.save_photo(user_id, frame_bytes, "jpg")

    monkeypatch.setattr(chat_attachments, "save_video_frame", fake_save_frame)

    async def fake_ask(user_id, question, history, **kwargs):
        return "Разбор готов."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 200, resp.text

    hist = await client.get("/ai/history")
    user_message = next(m for m in hist.json()["messages"] if m["role"] == "user")
    turn_id = user_message["image_url"].split("/")[3]

    image_resp = await client.get(f"/ai/history/{turn_id}/image")
    assert image_resp.status_code == 200
    assert image_resp.content == frame_bytes


@pytest.mark.asyncio
async def test_ask_video_frame_extraction_failure_does_not_block_the_answer(
    fresh_db, client_factory, monkeypatch, chat_media_dir
):
    """ffmpeg не смог достать кадр (битый файл, кодек) — тренер всё равно
    отвечает, просто без картинки в истории (см. save_video_frame → None)."""
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    async def fake_analyze(*a, **k):
        return _fake_analysis()

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)

    async def fake_ask(user_id, question, history, **kwargs):
        return "Разбор готов."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    # _video_data_url() кладёт заведомо не-видео байты — реальный ffmpeg
    # (chat_attachments.save_video_frame не подменяется здесь) честно не
    # достанет из них кадр и вернёт None.
    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 200, resp.text

    hist = await client.get("/ai/history")
    user_message = next(m for m in hist.json()["messages"] if m["role"] == "user")
    assert "image_url" not in user_message


@pytest.mark.asyncio
async def test_ask_video_rejects_someone_elses_exercise(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    stranger = await _linked_client(fresh_db, client_factory, telegram_id=222)
    created = await stranger.post("/exercises", json={"name": "Чужое упражнение"})
    stranger_exercise_id = created.json()["id"]

    resp = await owner.post(
        "/ai/video",
        json={"video_data_url": _video_data_url(), "exercise_id": stranger_exercise_id},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ask_video_rejects_too_big_payload(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "MAX_VIDEO_BYTES", 4)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 400
    assert resp.json()["error"] == "video_too_heavy"


@pytest.mark.asyncio
async def test_ask_video_rejects_too_long_duration(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "MAX_VIDEO_SECONDS", 5)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/video", json={"video_data_url": _video_data_url(), "duration_seconds": 30}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "video_too_long"


@pytest.mark.asyncio
async def test_ask_video_rejects_unsupported_format(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post(
        "/ai/video", json={"video_data_url": _video_data_url(mime="application/zip")}
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_ask_video_returns_502_when_analysis_fails(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        return None

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 502
    assert resp.json()["error"] == "video_analysis_failed"
    # Квота видео не должна списаться за неудавшийся разбор.
    assert await fresh_db.get_ai_video_count_today(111) == 0


@pytest.mark.asyncio
async def test_ask_video_respects_video_daily_limit(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_VIDEO_DAILY_LIMIT", 1)
    ai_limits.reset_cache()
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.increment_ai_video_count(111)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 429
    assert resp.json()["error"] == "video_limit_exceeded"


@pytest.mark.asyncio
async def test_ask_video_releases_busy_after_analysis_fails(fresh_db, client_factory, monkeypatch):
    """Провалившийся разбор (see test_ask_video_returns_502_when_analysis_fails)
    не должен оставлять человека заблокированным до конца суток — `finally`
    обязан снять `_busy` на любом исходе, включая исключение внутри try."""
    import api_v1_ai

    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fails(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        raise RuntimeError("Qwen3-VL is down")

    monkeypatch.setattr(video_analysis, "analyze", fails)
    client = await _linked_client(fresh_db, client_factory)

    # RuntimeError внутри try не перехватывается кодом ask_video — Starlette's
    # ServerErrorMiddleware отдаёт клиенту 500 (см. api_v1_common
    # .unhandled_error_handler), но ASGITransport по умолчанию (raise_app_exceptions)
    # пробрасывает исходное исключение и в тестовый httpx-клиент. Статус тут
    # не главное — важно, что бронь всё равно снята.
    with pytest.raises(RuntimeError, match="Qwen3-VL is down"):
        await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert 111 not in api_v1_ai._busy

    # И следующий, нормальный запрос после сбоя не натыкается на "занято".
    async def fake_analyze(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        return _fake_analysis()

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ после сбоя"

    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)
    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    resp = await client.post("/ai/video", json={"video_data_url": _video_data_url()})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_concurrent_video_asks_pay_for_analysis_only_once(fresh_db, client_factory, monkeypatch):
    """Тот же двойной-тап-сценарий, что у /ai/ask, но для /ai/video: без замка
    оба параллельных запроса дошли бы до Qwen3-VL (video_analysis.analyze) и
    до Grok (ai_trainer.ask) — двойной платёж за оба платных шага сразу."""
    import asyncio

    import api_v1_ai

    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    analyze_calls = 0
    release = asyncio.Event()

    async def slow_analyze(video_bytes, user_id, mime_type="video/mp4", exercise_hint=None):
        nonlocal analyze_calls
        analyze_calls += 1
        await release.wait()
        return _fake_analysis()

    async def fake_ask(user_id, question, history, **kwargs):
        return "ответ"

    monkeypatch.setattr(video_analysis, "analyze", slow_analyze)
    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    client = await _linked_client(fresh_db, client_factory)

    async def fire():
        return await client.post("/ai/video", json={"video_data_url": _video_data_url()})

    first_task = asyncio.ensure_future(fire())
    await asyncio.sleep(0.05)
    second_task = asyncio.ensure_future(fire())
    await asyncio.sleep(0.05)
    release.set()
    first_resp, second_resp = await asyncio.gather(first_task, second_task)

    statuses = sorted([first_resp.status_code, second_resp.status_code])
    assert statuses == [200, 429]
    assert analyze_calls == 1
    assert await fresh_db.get_ai_video_count_today(111) == 1
    assert 111 not in api_v1_ai._busy


# ---------- GET /ai/thinking ----------


@pytest.mark.asyncio
async def test_thinking_requires_auth(client_factory):
    client = client_factory()
    resp = await client.get("/ai/thinking")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_thinking_picks_topic_by_question(fresh_db, client_factory):
    """Тему угадывает сервер — в этом весь смысл ручки: клиент присылает голый
    текст вопроса и не знает ни про стемы, ни про список тем."""
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/thinking", params={"q": "сколько белка мне нужно в день"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["topic"] == running_texts.NUTRITION
    assert body["texts"] == running_texts.POOLS[running_texts.NUTRITION]
    assert body["texts"]


@pytest.mark.asyncio
async def test_thinking_without_question_falls_back_to_default_topic(fresh_db, client_factory):
    """Пустой/отсутствующий q — ровно то же, что classify(""): у приложения
    экран тренера открывается и без набранного вопроса."""
    client = await _linked_client(fresh_db, client_factory)

    for params in ({}, {"q": ""}):
        resp = await client.get("/ai/thinking", params=params)
        assert resp.status_code == 200
        body = resp.json()
        assert body["topic"] == running_texts.DEFAULT_TOPIC
        assert body["texts"] == running_texts.POOLS[running_texts.DEFAULT_TOPIC]


@pytest.mark.asyncio
async def test_thinking_speaks_the_users_language(fresh_db, client_factory):
    """Язык — из users.lang, как у остальных ручек: вопрос может быть на любом
    языке, а плейсхолдер обязан звучать на языке интерфейса."""
    client = await _linked_client(fresh_db, client_factory)
    await fresh_db.set_user_lang(111, "en")

    resp = await client.get("/ai/thinking", params={"q": "how much protein do i need"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["topic"] == running_texts.NUTRITION
    assert body["texts"] == running_texts.POOLS_EN[running_texts.NUTRITION]


@pytest.mark.asyncio
async def test_thinking_interval_matches_the_bot(fresh_db, client_factory):
    """Период ротации — один на бота и приложение, иначе плейсхолдер в двух
    клиентах живёт своей жизнью и правка в одном месте молча теряется."""
    from handlers import ai_trainer as ai_trainer_handlers

    client = await _linked_client(fresh_db, client_factory)

    resp = await client.get("/ai/thinking")
    assert resp.status_code == 200
    assert resp.json()["interval_seconds"] == ai_trainer_handlers.RUNNING_INTERVAL


# ---------- откат сделанного тренером (POST /ai/undo) ----------


def _fake_ask_logging_bodyweight(*weights: float):
    """fake ai_trainer.ask, ведущий себя как инструмент log_bodyweight: пишет
    вес в базу и отдаёт описание отката через on_action — ровно тем же
    словарём `{"label", "undo"}`, что и настоящий инструмент (ai_trainer.py,
    `{"kind": "bodyweight", "id": log_id}`)."""

    async def fake_ask(user_id, question, history, on_action=None, on_wire=None, **kwargs):
        for weight in weights:
            log_id = await db.add_bodyweight_log(user_id, weight)
            if on_action is not None:
                await on_action({"label": "↩️ Отменить", "undo": {"kind": "bodyweight", "id": log_id}})
        answer = f"Записал: {weights[0]:g}кг на сегодня. Если мимо — отменишь кнопкой ниже."
        if on_wire is not None:
            await on_wire(
                history
                + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            )
        return answer

    return fake_ask


@pytest.mark.asyncio
async def test_undo_requires_auth(client_factory):
    client = client_factory()
    resp = await client.post("/ai/undo", json={"key": "whatever"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ask_returns_undo_button_and_it_really_undoes(fresh_db, client_factory, monkeypatch):
    """Та самая жалоба: тренер пишет «отменишь кнопкой ниже», а кнопки нет.
    Описание отката инструмент отдавал и раньше — `collect_action` его собирал
    и выбрасывал, потому что в ответе ручки для него не было места."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client = await _linked_client(fresh_db, client_factory)

    ask = await client.post("/ai/ask", json={"question": "86.8 запиши вес за сегодня"})
    assert ask.status_code == 200, ask.text
    actions = ask.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["label"] == "↩️ Отменить"
    assert len(await fresh_db.list_bodyweight_logs(111)) == 1

    undo = await client.post("/ai/undo", json={"key": actions[0]["key"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["message"]
    assert await fresh_db.list_bodyweight_logs(111) == []


@pytest.mark.asyncio
async def test_undo_key_burns_after_first_use(fresh_db, client_factory, monkeypatch):
    """Двойной тап не должен снести ещё и вес, записанный ПОСЛЕ отката — то
    же, что у бота: ключ забирается до применения и навсегда."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client = await _linked_client(fresh_db, client_factory)

    key = (await client.post("/ai/ask", json={"question": "вес"})).json()["actions"][0]["key"]
    assert (await client.post("/ai/undo", json={"key": key})).status_code == 200

    await fresh_db.add_bodyweight_log(111, 87.2)
    again = await client.post("/ai/undo", json={"key": key})
    assert again.status_code == 404
    assert again.json()["error"] == "undo_gone"
    assert len(await fresh_db.list_bodyweight_logs(111)) == 1


@pytest.mark.asyncio
async def test_undo_is_private_per_user(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    key = (await client_a.post("/ai/ask", json={"question": "вес"})).json()["actions"][0]["key"]

    stolen = await client_b.post("/ai/undo", json={"key": key})
    assert stolen.status_code == 404
    assert len(await fresh_db.list_bodyweight_logs(111)) == 1


@pytest.mark.asyncio
async def test_several_undos_of_one_turn_fold_into_one_button(fresh_db, client_factory, monkeypatch):
    """«Удали всё из дневника» — это десятки вызовов с десятками откатов.
    Складываем их в одну кнопку, как `_fold_undo_actions` в боте, и тап
    возвращает всё, а не первую попавшуюся запись."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8, 87.0, 87.4))
    client = await _linked_client(fresh_db, client_factory)

    actions = (await client.post("/ai/ask", json={"question": "запиши три взвешивания"})).json()["actions"]
    assert len(actions) == 1
    assert len(await fresh_db.list_bodyweight_logs(111)) == 3

    assert (await client.post("/ai/undo", json={"key": actions[0]["key"]})).status_code == 200
    assert await fresh_db.list_bodyweight_logs(111) == []


@pytest.mark.asyncio
async def test_pending_restores_undo_button_after_reload(fresh_db, client_factory, monkeypatch):
    """Ушёл с экрана и вернулся — кнопка должна остаться на месте, как
    черновик программы и вопрос опросника."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client = await _linked_client(fresh_db, client_factory)

    key = (await client.post("/ai/ask", json={"question": "вес"})).json()["actions"][0]["key"]

    pending = await client.get("/ai/pending")
    assert pending.status_code == 200
    assert pending.json()["actions"] == [{"key": key, "label": "↩️ Отменить"}]

    assert (await client.post("/ai/undo", json={"key": key})).status_code == 200
    assert (await client.get("/ai/pending")).json()["actions"] == []


@pytest.mark.asyncio
async def test_new_conversation_drops_undo_buttons(fresh_db, client_factory, monkeypatch):
    """«Новый разговор» уносит ходы с экрана — вместе с ними должны уйти и
    ключи отката: кнопки, которой это можно было бы осознанно нажать, больше
    нет (в боте state.clear() уносит ai_undo ровно так же)."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client = await _linked_client(fresh_db, client_factory)

    key = (await client.post("/ai/ask", json={"question": "вес"})).json()["actions"][0]["key"]
    assert (await client.delete("/ai/history")).status_code == 200

    gone = await client.post("/ai/undo", json={"key": key})
    assert gone.status_code == 404
    assert len(await fresh_db.list_bodyweight_logs(111)) == 1


@pytest.mark.asyncio
async def test_undo_reports_failure_when_row_already_gone(fresh_db, client_factory, monkeypatch):
    """Запись убрали руками (или из другого клиента) — честный отказ, а не
    молчаливое «готово»: вернуть уже нечего."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_logging_bodyweight(86.8))
    client = await _linked_client(fresh_db, client_factory)

    key = (await client.post("/ai/ask", json={"question": "вес"})).json()["actions"][0]["key"]
    log_id = (await fresh_db.list_bodyweight_logs(111))[0]["id"]
    await fresh_db.delete_bodyweight_log(log_id, 111)

    resp = await client.post("/ai/undo", json={"key": key})
    assert resp.status_code == 409
    assert resp.json()["error"] == "undo_failed"


# ---------- архив прошлых разговоров (GET /ai/conversations) ----------


@pytest.mark.asyncio
async def test_conversations_requires_auth(client_factory):
    client = client_factory()
    assert (await client.get("/ai/conversations")).status_code == 401
    assert (await client.get("/ai/conversations/1")).status_code == 401


@pytest.mark.asyncio
async def test_new_conversation_archives_instead_of_erasing(fresh_db, client_factory, monkeypatch):
    """«Новый разговор» раньше стирал ходы насовсем — перечитать «а что я тогда
    спрашивал» было негде. Теперь экран чата пуст так же, как и был, но сам
    разговор лежит архивом."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "как мой прогресс"})
    await client.post("/ai/ask", json={"question": "а белок"})
    assert (await client.delete("/ai/history")).status_code == 200

    assert (await client.get("/ai/history")).json()["messages"] == []

    listing = await client.get("/ai/conversations")
    assert listing.status_code == 200
    body = listing.json()
    assert body["current"] == 2
    assert len(body["conversations"]) == 1
    archived = body["conversations"][0]
    assert archived["id"] == 1
    assert archived["turns"] == 2
    # Заголовок — первый вопрос разговора: своего имени у разговора нет.
    assert archived["title"] == "как мой прогресс"

    messages = (await client.get(f"/ai/conversations/{archived['id']}")).json()["messages"]
    assert [m["text"] for m in messages] == [
        "как мой прогресс", "ответ на: как мой прогресс",
        "а белок", "ответ на: а белок",
    ]


@pytest.mark.asyncio
async def test_new_conversation_still_starts_the_model_from_scratch(
    fresh_db, client_factory, monkeypatch
):
    """Главное, ради чего «новый разговор» вообще есть: испорченный контекст не
    должен доехать до модели. Архив — это про экран, а не про промпт."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    seen: list[list] = []

    async def fake_ask(user_id, question, history, on_wire=None, **kwargs):
        seen.append(history)
        answer = f"ответ на: {question}"
        if on_wire is not None:
            await on_wire(
                history
                + [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            )
        return answer

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "первый"})
    await client.delete("/ai/history")
    await client.post("/ai/ask", json={"question": "после сброса"})

    assert seen[0] == []
    assert seen[1] == [], "в новый разговор утёк контекст старого"


@pytest.mark.asyncio
async def test_archive_survives_a_long_new_conversation(fresh_db, client_factory, monkeypatch):
    """Подрезка до MAX_AI_CONVERSATION_TURNS считает ходы ВНУТРИ разговора:
    иначе длинный новый разговор молча выел бы архив прошлого."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    # Ходов тут больше, чем вопросов в дневной квоте: про подрезку истории, а
    # не про лимит — его проверяют свои тесты выше.
    monkeypatch.setattr(config, "AI_QUESTION_DAILY_LIMIT", 100)
    ai_limits.reset_cache()
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "старый разговор"})
    await client.delete("/ai/history")
    for i in range(db.MAX_AI_CONVERSATION_TURNS + 2):
        await client.post("/ai/ask", json={"question": f"вопрос {i}"})

    archived = (await client.get("/ai/conversations/1")).json()["messages"]
    assert [m["text"] for m in archived] == ["старый разговор", "ответ на: старый разговор"]
    # А сам текущий разговор подрезан, как и был.
    assert len((await client.get("/ai/history")).json()["messages"]) == 2 * db.MAX_AI_CONVERSATION_TURNS


@pytest.mark.asyncio
async def test_archive_is_private_per_user(fresh_db, client_factory, monkeypatch):
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    await client_a.post("/ai/ask", json={"question": "мой личный вопрос"})
    await client_a.delete("/ai/history")

    assert (await client_b.get("/ai/conversations")).json()["conversations"] == []
    assert (await client_b.get("/ai/conversations/1")).json()["messages"] == []


@pytest.mark.asyncio
async def test_retention_drops_old_archive_but_never_the_current_talk(
    fresh_db, client_factory, monkeypatch
):
    """Архив растёт на каждый новый разговор, поэтому чистится ночным джобом.
    Текущий разговор он не трогает, даже если тот давно без движения: он
    открыт на экране."""
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "ask", _fake_ask_with_wire())
    client = await _linked_client(fresh_db, client_factory)

    await client.post("/ai/ask", json={"question": "давний разговор"})
    await client.delete("/ai/history")
    await client.post("/ai/ask", json={"question": "тоже давний, но текущий"})

    long_ago = (dt.date.today() - dt.timedelta(days=400)).isoformat() + "T10:00:00"
    await fresh_db.conn().execute(
        "UPDATE ai_conversation_turns SET created_at = ?", (long_ago,)
    )
    await fresh_db.conn().commit()

    removed = await fresh_db.prune_old_ai_conversations(180)
    assert removed == 1
    assert (await client.get("/ai/conversations/1")).json()["messages"] == []
    assert len((await client.get("/ai/history")).json()["messages"]) == 2
