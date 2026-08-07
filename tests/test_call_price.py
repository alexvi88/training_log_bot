"""config.call_price_usd: одна формула цены на лог и на дневной отчёт.

Строка в логе печатает цену каждого запроса сразу — чтобы смотреть расход, не
дожидаясь ночного отчёта. Отчёт считает сумму за сутки. Формула у них обязана
быть общей, иначе на один вопрос будет два разных ответа.
"""

import pytest

import admin_tasks
import config


def test_price_matches_table():
    # grok-4.5-latest: $0.002/$0.006 за 1K.
    assert config.call_price_usd("grok-4.5-latest", 1000, 1000) == pytest.approx(0.008)


def test_video_model_priced_from_its_own_row():
    """$0.3/$1.5 за 1M — то есть 24k входа и 900 выхода дают около полутора центов."""
    price = config.call_price_usd(config.NOVITA_VIDEO_MODEL, 24_000, 900)
    assert price == pytest.approx(24_000 / 1000 * 0.0003 + 900 / 1000 * 0.0015)
    # Порядок величины: разбор видео стоит центы, а не доллары.
    assert 0.001 < price < 0.05


def test_unknown_model_falls_back_to_default():
    inp, out = config.DEFAULT_LLM_PRICE_USD_PER_1K
    assert config.call_price_usd("нет-такой-модели", 1000, 1000) == pytest.approx(inp + out)


def test_cached_input_is_cheaper_than_fresh(monkeypatch):
    """По прайсу grok-4.5: вход $2/1M, кэшированный $0.30/1M — то есть 0.15."""
    monkeypatch.setattr(config, "CACHED_INPUT_PRICE_MULTIPLIER", 0.15)
    full = config.call_price_usd("grok-4.5-latest", 10_000, 0)
    all_cached = config.call_price_usd("grok-4.5-latest", 10_000, 0, cached_tokens=10_000)
    assert all_cached == pytest.approx(full * 0.15)


def test_partially_cached_prompt_splits_by_rate(monkeypatch):
    """Постоянная шапка из кэша, свежий хвост по полной — так и уезжает каждый раунд."""
    monkeypatch.setattr(config, "CACHED_INPUT_PRICE_MULTIPLIER", 0.15)
    inp, _out = config.LLM_PRICES_USD_PER_1K["grok-4.5-latest"]
    price = config.call_price_usd("grok-4.5-latest", 14_338, 0, cached_tokens=11_000)
    expected = 3_338 / 1000 * inp + 11_000 / 1000 * inp * 0.15
    assert price == pytest.approx(expected)


def test_cached_tokens_cannot_exceed_prompt(monkeypatch):
    """Провайдер соврал — цена всё равно не должна уйти в минус."""
    monkeypatch.setattr(config, "CACHED_INPUT_PRICE_MULTIPLIER", 0.15)
    price = config.call_price_usd("grok-4.5-latest", 100, 0, cached_tokens=999_999)
    assert price > 0


def test_reasoning_tokens_billed_as_output():
    """У xAI это отдельный billable тип, в completion_tokens не входит — раньше
    мы его не считали вовсе и занижали стоимость."""
    _inp, out = config.LLM_PRICES_USD_PER_1K["grok-4.5-latest"]
    without = config.call_price_usd("grok-4.5-latest", 0, 500)
    with_reasoning = config.call_price_usd("grok-4.5-latest", 0, 500, reasoning_tokens=1_000)
    assert with_reasoning - without == pytest.approx(1_000 / 1000 * out)


def test_daily_report_agrees_with_per_call_price():
    """Сумма отчёта = сумма цен вызовов. Тест ловит расхождение двух копий формулы."""
    breakdown = {
        "grok-4.5-latest": {"calls": 2, "prompt_tokens": 12_000, "completion_tokens": 800},
        config.NOVITA_VIDEO_MODEL: {"calls": 1, "prompt_tokens": 24_000, "completion_tokens": 900},
    }
    report_cost, calls, tokens = admin_tasks._llm_cost(breakdown)
    expected = sum(
        config.call_price_usd(model, s["prompt_tokens"], s["completion_tokens"])
        for model, s in breakdown.items()
    )
    assert report_cost == pytest.approx(expected)
    assert calls == 3
    assert tokens == 12_000 + 800 + 24_000 + 900
