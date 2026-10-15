"""Окно истории тренера: обрезка ступенькой и заметка о выброшенном.

Раньше историю резали впритык к AI_WIRE_HISTORY_MAX_CHARS, и после первой же
обрезки она стояла у самого лимита: каждый следующий вопрос снова его
переступал, снова менял начало истории — и платил промахом кэша по всему
запросу (прод: 66214 токенов, из кэша 384, $0.135 за ответ вместо ~$0.02).

Здесь держим то, что делает кэш возможным:
- до верхнего порога окно не трогается, следующий запрос начинается байт в
  байт с предыдущего (и у grok, и у Luna);
- переступив порог, окно режется сразу до нижнего и дальше снова только
  дописывается;
- срез идёт по границе целого хода — без осиротевших tool-результатов и без
  рассуждений Luna, оторванных от своего сообщения;
- заметка о выброшенном встаёт первым сообщением и не меняется до следующего
  среза; её сбой ход не ломает;
- новый разговор начинается без окна и без заметки.
"""

import json
import logging
from types import SimpleNamespace

import pytest

import ai_limits
import ai_trainer
import config
import db
import i18n

pytestmark = pytest.mark.asyncio

UPPER = 4000
LOWER = 2200


@pytest.fixture(autouse=True)
def _small_window(monkeypatch):
    monkeypatch.setattr(config, "AI_WIRE_HISTORY_MAX_CHARS", UPPER)
    monkeypatch.setattr(config, "AI_WIRE_HISTORY_TRIM_TO_CHARS", LOWER)


def _turn(n: int, *, filler: int = 300, tool: bool = True) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": f"вопрос {n}"}]
    if tool:
        msgs += [
            {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{n}", "type": "function",
                                "function": {"name": "get_training_overview", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": f"c{n}", "content": "д" * filler},
        ]
    msgs.append({"role": "assistant", "content": f"ответ {n}", "reasoning_content": "думал"})
    return msgs


def _history(turns: int, **kw) -> list[dict]:
    return sum((_turn(i, **kw) for i in range(turns)), [])


def _size(msgs) -> int:
    return sum(ai_trainer._message_wire_chars(None, m) for m in msgs)


# ---------- фейковый grok: главный вызов и вызов сводки ----------


def _is_summary_call(kwargs) -> bool:
    first = kwargs["messages"][0].get("content") or ""
    return first.startswith(ai_trainer.HISTORY_SUMMARY_SYSTEM_PROMPT[:40])


class _Grok:
    def __init__(self, summary="- колено болит, приседаем до параллели", fail_summary=False):
        self.main: list[dict] = []
        self.summaries: list[dict] = []
        self.summary = summary
        self.fail_summary = fail_summary
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        if _is_summary_call(kwargs):
            self.summaries.append(kwargs)
            if self.fail_summary:
                raise RuntimeError("провайдер лёг")
            text = self.summary
        else:
            self.main.append(kwargs)
            text = f"ответ на ход {len(self.main)}"
        message = SimpleNamespace(content=text, tool_calls=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def _ask(user_id, question, history):
    seen: list = []

    async def store(msgs):
        seen.append(msgs)

    await ai_trainer.ask(user_id, question, history, on_wire=store)
    return seen[0]


# ---------- окно стабильно до порога ----------


async def test_under_threshold_window_is_the_same_object_and_no_summary(monkeypatch):
    history = _history(2)
    called = []
    monkeypatch.setattr(ai_trainer, "_summarize_dropped", lambda *a: called.append(a))

    window = await ai_trainer._step_trimmed_history(1, history, ai_trainer.grok_backend())

    assert window is history
    assert called == []


async def test_grok_next_request_extends_previous_byte_for_byte(fresh_db, user_id, monkeypatch):
    """Ниже порога каждый следующий запрос начинается ровно с того, что ушло в
    прошлый раз, — шапка, прошлые ходы, всё."""
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    history: list = []
    for q in ("первый", "второй", "третий"):
        history = await _ask(user_id, q, history)

    assert grok.summaries == []
    first, second, third = (json.dumps(c["messages"], ensure_ascii=False) for c in grok.main)
    assert second.startswith(first[:-1]) and third.startswith(second[:-1])
    for earlier, later in zip(grok.main, grok.main[1:], strict=False):
        n = len(earlier["messages"])
        assert later["messages"][:n] == earlier["messages"]
        assert later.get("tools") == earlier.get("tools")


# ---------- срез ступенькой ----------


async def test_over_threshold_cuts_down_to_lower_with_summary(fresh_db, user_id, monkeypatch, caplog):
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)
    history = _history(12)
    assert _size(history) > UPPER

    with caplog.at_level(logging.INFO, logger="ai_trainer"):
        window = await ai_trainer._step_trimmed_history(user_id, history, ai_trainer.grok_backend())

    summary, *kept = window
    assert summary[ai_trainer.HISTORY_SUMMARY_KEY] is True
    assert summary["role"] == "system"
    assert "колено болит" in summary["content"]
    assert kept[0]["role"] == "user"
    assert _size(kept) <= LOWER
    # Ступенька, а не впритык: места ещё на несколько ходов.
    assert _size(window) < UPPER - 3 * _size(_turn(99))
    assert len(grok.summaries) == 1
    line = next(r.getMessage() for r in caplog.records if "step-trim" in r.getMessage())
    assert f"step-trim {len(history)}→{len(window)}, summary " in line


async def test_window_and_summary_stay_put_until_the_next_cut(fresh_db, user_id, monkeypatch):
    """После среза ходы снова только дописываются — заметка та же, начало то же,
    сводку второй раз не зовём."""
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    history = await _ask(user_id, "вопрос после длинного разговора", _history(12))
    assert len(grok.summaries) == 1
    for q in ("ещё", "и ещё"):
        before = history
        history = await _ask(user_id, q, history)
        assert history[: len(before)] == before

    assert len(grok.summaries) == 1
    summary_msgs = [m for c in grok.main for m in c["messages"] if "колено болит" in (m.get("content") or "")]
    assert len(summary_msgs) == 3 and all(m == summary_msgs[0] for m in summary_msgs)
    # Заметка ушла провайдеру системным сообщением сразу за шапкой, без нашей пометки.
    for call in grok.main:
        assert call["messages"][1]["role"] == "system"
        assert "колено болит" in call["messages"][1]["content"]
        assert all(ai_trainer.HISTORY_SUMMARY_KEY not in m for m in call["messages"])
    n = len(grok.main[0]["messages"]) - 1  # без вопроса хода: он у каждого свой
    assert grok.main[1]["messages"][:n] == grok.main[0]["messages"][:n]


async def test_second_cut_folds_old_summary_into_the_new_one(fresh_db, user_id, monkeypatch):
    grok = _Grok(summary="- новая заметка")
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)
    old = ai_trainer._history_summary_message("- колено болит с весны")
    history = [old, *_history(12)]

    window = await ai_trainer._step_trimmed_history(user_id, history, ai_trainer.grok_backend())

    (call,) = grok.summaries
    prompt = call["messages"][1]["content"]
    assert "колено болит с весны" in prompt  # прошлая заметка — на вход новой
    assert "вопрос 0" in prompt  # и выброшенный кусок тоже
    assert ai_trainer._HISTORY_SUMMARY_HEADER not in prompt  # заголовок не копится
    assert window[0]["content"].endswith("- новая заметка")
    assert sum(1 for m in window if m.get(ai_trainer.HISTORY_SUMMARY_KEY)) == 1


async def test_summary_failure_still_cuts_and_answers(fresh_db, user_id, monkeypatch, caplog):
    grok = _Grok(fail_summary=True)
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    with caplog.at_level(logging.WARNING, logger="ai_trainer"):
        wire = await _ask(user_id, "вопрос", _history(12))

    assert len(grok.main) == 1, "ответ атлету состоялся"
    assert wire[0]["role"] == "user", "срез без заметки"
    assert _size(wire) < UPPER
    assert any("summary failed" in r.getMessage() for r in caplog.records)


async def test_summary_failure_keeps_the_old_summary(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _Grok(fail_summary=True))
    old = ai_trainer._history_summary_message("- колено болит с весны")

    window = await ai_trainer._step_trimmed_history(
        user_id, [old, *_history(12)], ai_trainer.grok_backend()
    )

    assert window[0] is old
    assert window[1]["role"] == "user"


async def test_hard_stop_skips_the_summary_but_still_cuts(fresh_db, user_id, monkeypatch):
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    async def hard():
        return ai_limits.Block(kind=ai_limits.KIND_SPEND_HARD, log="hard")

    monkeypatch.setattr(ai_limits, "hard_stop_block", hard)
    window = await ai_trainer._step_trimmed_history(user_id, _history(12), ai_trainer.grok_backend())

    assert grok.summaries == []
    assert window[0]["role"] == "user"


async def test_summary_call_is_priced_and_has_its_own_cache_slot(fresh_db, user_id, monkeypatch):
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)
    logged = []

    async def log_cost(uid, model, usage, *, source="bot"):
        logged.append((uid, model))

    monkeypatch.setattr(ai_trainer, "_log_llm_cost", log_cost)
    await ai_trainer._step_trimmed_history(user_id, _history(12), ai_trainer.grok_backend())

    (call,) = grok.summaries
    assert "tools" not in call
    assert call["extra_headers"] == {"x-grok-conv-id": f"history_summary-{user_id}"}
    assert call["extra_body"]["reasoning_effort"] == config.GROK_QUICK_REASONING_EFFORT
    assert logged == [(user_id, config.GROK_MODEL)]


async def test_summary_speaks_the_athletes_language(fresh_db, user_id, monkeypatch):
    grok = _Grok()
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: grok)

    with i18n.use_lang("en"):
        await ai_trainer._step_trimmed_history(user_id, _history(12), ai_trainer.grok_backend())
    with i18n.use_lang("ru"):
        await ai_trainer._step_trimmed_history(user_id, _history(12), ai_trainer.grok_backend())

    en, ru = (c["messages"][0]["content"] for c in grok.summaries)
    assert en.endswith(i18n.t_in("en", "ai.language_tail"))
    assert ru.endswith(i18n.t_in("ru", "ai.language_tail"))


async def test_summary_is_clipped_even_if_the_model_rambles(fresh_db, user_id, monkeypatch):
    monkeypatch.setattr(ai_trainer, "_get_client", lambda: _Grok(summary="- пункт\n" * 1000))
    window = await ai_trainer._step_trimmed_history(user_id, _history(12), ai_trainer.grok_backend())
    assert len(ai_trainer._history_summary_text(window[0])) <= ai_trainer._SUMMARY_MAX_CHARS


# ---------- срез по границе хода ----------


async def test_cut_never_orphans_tools_and_takes_turn_context_along():
    """Начало окна — вопрос атлета вместе с системной вставкой его хода (видео);
    каждый tool — со своим вызовом в том же окне."""
    history: list = []
    for i in range(12):
        if i % 3 == 0:
            history.append({"role": "system", "content": f"наблюдения по видео {i}"})
        history += _turn(i)
        if i % 4 == 0:
            # Объяснение программы посреди хода — системная вставка не перед вопросом.
            history.insert(len(history) - 1, {"role": "system", "content": "объясни черновик"})

    for lower in range(300, 3000, 97):
        plan = ai_trainer._plan_step_trim(history, None, UPPER, lower)
        _summary, dropped, kept = plan
        assert dropped + kept == history
        assert kept[0]["role"] in ("user", "system")
        if kept[0]["role"] == "system":
            assert kept[0]["content"].startswith("наблюдения по видео")
            assert kept[1]["role"] == "user"
        assert ai_trainer._self_contained(kept)
        assert all(m.get("role") != "tool" for m in kept[:1])


async def test_luna_cut_keeps_reasoning_with_its_message_and_calls():
    """У Luna рассуждение и вызов едут одним assistant-сообщением
    (OPENAI_OUTPUT_KEY) — срез идёт только перед вопросом, так что они вместе."""
    history: list = []
    for i in range(10):
        reasoning = {"type": "reasoning", "id": f"rs_{i}", "encrypted_content": "з" * 200, "summary": []}
        call = {"type": "function_call", "call_id": f"c{i}", "name": "get_training_overview", "arguments": "{}"}
        history += [
            {"role": "user", "content": f"вопрос {i}"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "get_training_overview", "arguments": "{}"}}
            ], ai_trainer.OPENAI_OUTPUT_KEY: [reasoning, call]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "д" * 200},
            {"role": "assistant", "content": f"ответ {i}", ai_trainer.OPENAI_OUTPUT_KEY: [
                {"type": "reasoning", "id": f"rs_f{i}", "encrypted_content": "з" * 200, "summary": []},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"ответ {i}"}]},
            ]},
        ]
    luna = SimpleNamespace(is_openai=True)
    _summary, _dropped, kept = ai_trainer._plan_step_trim(history, luna, 4000, 2000)

    items = ai_trainer._responses_input(kept)
    assert items[0] == {"role": "user", "content": kept[0]["content"]}
    outputs = {i["call_id"] for i in items if i.get("type") == "function_call_output"}
    calls = {i["call_id"] for i in items if i.get("type") == "function_call"}
    assert outputs <= calls
    # Каждое рассуждение — сразу перед своим вызовом или сообщением, как пришло.
    for a, b in zip(items, items[1:], strict=False):
        if a.get("type") == "reasoning":
            assert b.get("type") in ("function_call", "message")


async def test_luna_size_counts_what_is_sent_not_the_stored_double():
    """Ход Luna хранит текст ответа дважды (content и сырой output) — уезжает
    один раз. Мерить словарь целиком значило бы резать раньше времени."""
    text = "о" * 1000
    msg = {"role": "assistant", "content": text, ai_trainer.OPENAI_OUTPUT_KEY: [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    ]}
    luna = SimpleNamespace(is_openai=True)
    assert ai_trainer._message_wire_chars(luna, msg) < 1200
    assert ai_trainer._message_wire_chars(None, msg) < 1200


# ---------- Luna: те же input-элементы между ходами ----------


def _luna_post(sink: list, summaries: list):
    async def post(path, *, body, cast_to, **kwargs):
        assert path == "/responses"
        sink.append(body)
        n = len(sink)
        output = [
            {"type": "reasoning", "id": f"rs_{n}", "encrypted_content": "з" * 300, "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "о" * 300}]},
        ]
        return {"output": output, "usage": {"input_tokens": 100, "output_tokens": 10}}

    async def create(**kwargs):
        summaries.append(kwargs)
        message = SimpleNamespace(content="- спина: без становой", tool_calls=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(post=post, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_luna_input_prefix_is_stable_and_cut_is_a_step(fresh_db, monkeypatch):
    owner = 111
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "TEST_USER_ID", owner)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    await db.get_or_create_user(telegram_id=owner, username="own")
    assert await db.set_user_ai_model(owner, "luna")
    bodies: list = []
    summaries: list = []
    monkeypatch.setattr(ai_trainer, "_get_openai_chat_client", lambda: _luna_post(bodies, summaries))

    history: list = []
    for i in range(14):
        history = await _ask(owner, f"вопрос {i}", history)

    assert summaries, "окно так и не переступило верхний порог"
    cuts = 0
    for earlier, later in zip(bodies, bodies[1:], strict=False):
        n = len(earlier["input"])
        if later["input"][:n] == earlier["input"]:
            continue  # дописали — кэш живёт
        cuts += 1
        # Срез: шапка та же, сразу за ней — заметка системным сообщением без
        # нашей пометки, дальше вопрос.
        assert later["input"][0] == earlier["input"][0]
        assert later["input"][1] == {"role": "system", "content": later["input"][1]["content"]}
        assert "спина: без становой" in later["input"][1]["content"]
        assert later["input"][2]["role"] == "user"
    assert cuts == len(summaries)
    assert cuts < (len(bodies) - 1) / 2, "срез ступенькой, а не на каждом ходе"
    assert summaries[0]["model"] == "gpt-6-luna"
    assert summaries[0]["extra_body"]["prompt_cache_key"] == f"history_summary-{owner}"
    assert all(ai_trainer.HISTORY_SUMMARY_KEY not in item for b in bodies for item in b["input"])


# ---------- новый разговор ----------


async def test_new_conversation_starts_without_window_or_summary(fresh_db, user_id):
    wire = [ai_trainer._history_summary_message("- колено"), *_turn(1)]
    await db.add_ai_conversation_turn(user_id, "вопрос", "ответ", wire)
    assert (await db.get_ai_conversation_wire_history(user_id))[0][ai_trainer.HISTORY_SUMMARY_KEY]

    await db.start_new_ai_conversation(user_id)

    assert await db.get_ai_conversation_wire_history(user_id) == []


async def test_bot_new_chat_drops_the_window_with_its_summary():
    """В боте окно (и заметка в нём) живёт в ai_history — «Новый разговор»
    обнуляет его вместе с остальными ключами разговора."""
    from handlers import ai_trainer as handler

    assert "ai_history" in handler._CONVERSATION_STATE_KEYS
