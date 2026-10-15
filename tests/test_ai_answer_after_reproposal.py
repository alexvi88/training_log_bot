"""Ответ тренера после того, как он пересобрал программу за один ход.

Живой прогон через REST `/v1/ai/ask`: модель трижды вызвала propose_program,
урезая подходы под недельный потолок, а атлет получил ответом только реплики
между вызовами, склеенные без пробела, — «Ещё один подход срежу с задней
дельты — и готово.Срежу один подход с **тяги к лицу** — плечи встанут в
потолок.» — и ни слова о самой программе. Черновик при этом был верный.

Фейковая модель здесь повторяет ровно этот ход: два propose_program с
промежуточным текстом, а финальным раундом — те же две реплики слитно.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import ai_trainer
import api_v1

pytestmark = pytest.mark.asyncio

INTERIM_1 = "Ещё один подход срежу с задней дельты — и готово."
INTERIM_2 = "Срежу один подход с **тяги к лицу** — плечи встанут в потолок."
EXPLANATION = (
    "Верх/низ на четыре дня: тяжёлые базовые в начале дня, изоляция в конце. "
    "Прогрессия двойная — взял верх диапазона во всех подходах, добавь 2.5 кг. "
    "Раз в пять недель разгрузка. Программа ждёт подтверждения под сообщением."
)


def _program(sets: int) -> dict:
    return {
        "name": "Верх/низ",
        "days": [
            {"name": "Верх", "exercises": [
                {"name": "Жим штанги лёжа", "sets": sets, "reps_min": 6, "reps_max": 10},
            ]},
            {"name": "Низ", "exercises": [
                {"name": "Присед со штангой", "sets": sets, "reps_min": 6, "reps_max": 10},
            ]},
        ],
    }


def _propose(call_id: str, sets: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name="propose_program", arguments=json.dumps(_program(sets))),
    )


def _response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _reproposing_client(final_rounds: list) -> SimpleNamespace:
    """Модель из прогона: два propose_program с репликой перед каждым, дальше —
    final_rounds по порядку."""
    responses = [
        _response(content=INTERIM_1, tool_calls=[_propose("call_1", 4)]),
        _response(content=INTERIM_2, tool_calls=[_propose("call_2", 3)]),
        *final_rounds,
    ]
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=responses)))
    )


async def test_glued_interim_chatter_is_not_the_answer(fresh_db, user_id, monkeypatch):
    client = _reproposing_client([
        _response(content=INTERIM_1 + INTERIM_2),
        _response(content=EXPLANATION),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    drafts: list[dict] = []
    wire: list[list] = []

    async def on_program(draft):
        drafts.append(draft)

    async def on_wire(messages):
        wire.append(messages)

    answer = await ai_trainer._ask_plain(
        user_id, "Собери программу", history=[], on_program=on_program, on_wire=on_wire,
    )

    assert answer == EXPLANATION
    assert INTERIM_1 not in answer and INTERIM_2 not in answer
    assert drafts  # черновик как был, так и остался
    create = client.chat.completions.create
    assert create.await_count == 4
    # Дополнительный шаг ДОПИСЫВАЕТ разговор: всё, что ушло в прошлом запросе,
    # — неизменный префикс нового, и набор инструментов тот же (кэш xAI).
    before = create.await_args_list[2].kwargs
    after = create.await_args_list[3].kwargs
    assert after["messages"][: len(before["messages"])] == before["messages"]
    assert after["messages"][-1]["role"] == "system"
    assert after.get("tools") == before.get("tools")
    # История следующего хода кончается объяснением, а не репликами.
    assert wire[-1][-1] == {"role": "assistant", "content": EXPLANATION}


async def test_interim_chatter_is_cut_from_a_real_explanation(fresh_db, user_id, monkeypatch):
    """Объяснение есть, но к нему прилипли реплики про прошлые черновики —
    лишний шаг не нужен, нужно только вырезать прилипшее."""
    client = _reproposing_client([_response(content=INTERIM_2 + EXPLANATION)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(user_id, "Собери программу", history=[])

    assert answer == EXPLANATION
    assert client.chat.completions.create.await_count == 3


async def test_a_good_answer_after_reproposal_costs_no_extra_call(fresh_db, user_id, monkeypatch):
    client = _reproposing_client([_response(content=EXPLANATION)])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)

    answer = await ai_trainer._ask_plain(user_id, "Собери программу", history=[])

    assert answer == EXPLANATION
    assert client.chat.completions.create.await_count == 3


# ---------- Telegram: тот же ход со стримингом в черновик ----------


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


def _delta(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _streamed_propose(call_id: str, sets: int) -> SimpleNamespace:
    return SimpleNamespace(
        index=0, id=call_id,
        function=SimpleNamespace(name="propose_program", arguments=json.dumps(_program(sets))),
    )


async def test_streamed_turn_gets_the_explanation_too(fresh_db, user_id, monkeypatch):
    streams = [
        _FakeStream([_delta(INTERIM_1), _delta(tool_calls=[_streamed_propose("call_1", 4)])]),
        _FakeStream([_delta(INTERIM_2), _delta(tool_calls=[_streamed_propose("call_2", 3)])]),
        _FakeStream([_delta(INTERIM_1), _delta(INTERIM_2)]),
        _FakeStream([_delta(EXPLANATION[:60]), _delta(EXPLANATION[60:])]),
    ]
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=streams)))
    )
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    monkeypatch.setattr(ai_trainer, "STREAM_FLUSH_SECONDS", 0)
    monkeypatch.setattr(ai_trainer, "MIN_FIRST_FLUSH_CHARS", 0)
    chunks: list[str] = []

    async def on_chunk(text):
        chunks.append(text)

    answer = await ai_trainer._ask_plain(user_id, "Собери программу", history=[], on_chunk=on_chunk)

    assert answer == EXPLANATION
    # Черновик в Telegram последним показал объяснение, а не реплику.
    assert chunks[-1] == EXPLANATION
    assert client.chat.completions.create.await_count == 4


# ---------- REST /v1/ai/ask ----------


async def test_rest_ask_answers_with_the_final_program_explanation(fresh_db, monkeypatch):
    client = _reproposing_client([
        _response(content=INTERIM_1 + INTERIM_2),
        _response(content=EXPLANATION),
    ])
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: client)
    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    transport = httpx.ASGITransport(app=api_v1.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post("/auth/link", json={"code": code})
        http.headers["Authorization"] = f"Bearer {resp.json()['token']}"

        resp = await http.post("/ai/ask", json={"question": "Собери программу"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == EXPLANATION
    assert body["program"] is not None
