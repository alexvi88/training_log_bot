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
    """Текущая модель разбора — thinking-вариант, $0.98/$3.95 за 1M.

    24k входа и 900 выхода дают около 2.7 цента, и это ещё БЕЗ рассуждения:
    reasoning-токены тарифицируются как выход и обычно длиннее самого JSON,
    так что живой чек выйдет заметно больше. На instruct та же арифметика
    давала полтора цента — разница и есть цена внимательности.
    """
    inp, out = config.LLM_PRICES_USD_PER_1K[config.NOVITA_VIDEO_MODEL]
    price = config.call_price_usd(config.NOVITA_VIDEO_MODEL, 24_000, 900)
    assert price == pytest.approx(24_000 / 1000 * inp + 900 / 1000 * out)
    # Порядок величины: разбор видео стоит центы, а не доллары.
    assert 0.001 < price < 0.10


def test_the_video_model_has_its_own_price_row():
    """Модель без своей строки считается по ДЕФОЛТНОЙ пессимистичной ставке, и
    дневной отчёт по деньгам начинает врать молча. Переключили модель — обязаны
    завести ей цену."""
    assert config.NOVITA_VIDEO_MODEL in config.LLM_PRICES_USD_PER_1K


def test_unknown_model_falls_back_to_default():
    inp, out = config.DEFAULT_LLM_PRICE_USD_PER_1K
    assert config.call_price_usd("нет-такой-модели", 1000, 1000) == pytest.approx(inp + out)


def test_default_price_is_the_most_expensive_known_rate():
    """Модель не в таблице обязана считаться ДОРОГО, а не дёшево.

    Дефолт когда-то был прибит к ставке grok-4-1-fast — самой дешёвой строке
    таблицы, к тому же снятой моделью. Переключись мы на новую модель, забыв
    добавить её в прайс, — лог и дневной отчёт занизили бы расход в десять раз и
    молча: занижение выглядит как хорошая новость, его не идут проверять.
    """
    inp, out = config.DEFAULT_LLM_PRICE_USD_PER_1K
    for model, (known_inp, known_out) in config.LLM_PRICES_USD_PER_1K.items():
        assert inp >= known_inp, f"дефолт дешевле входа {model}"
        assert out >= known_out, f"дефолт дешевле выхода {model}"
    # Любой известный вызов не дороже такого же вызова по дефолтной ставке.
    for model in config.LLM_PRICES_USD_PER_1K:
        known = config.call_price_usd(model, 10_000, 1_000)
        assert known <= config.call_price_usd("нет-такой-модели", 10_000, 1_000)


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


def test_grok_43_price_matches_the_published_rates():
    """docs.x.ai/developers/models/grok-4.3: вход $1.25/1M, выход $2.50/1M."""
    assert config.LLM_PRICES_USD_PER_1K["grok-4.3-latest"] == (0.00125, 0.0025)


# Цена кэшированного входа по docs.x.ai, $ за 1K токенов. В таблицу config она
# не входит (там только вход/выход), а для проверки доли нужна.
CACHED_INPUT_PRICE_USD_PER_1K = {
    "grok-4.5-latest": 0.0003,   # $0.30 за 1M
    "grok-4.3-latest": 0.0002,   # $0.20 за 1M
}


def test_cache_share_matches_the_model_we_actually_run():
    """Доля кэша одна на все модели, а прайс у каждой свой: у 4.5 это 0.15
    ($0.30 при $2.00), у 4.3 — 0.16 ($0.20 при $1.25). Переехали моделью, а долю
    забыли — экономия в логах поедет молча, и заметить это нечем."""
    model = config.GROK_MODEL
    if model not in CACHED_INPUT_PRICE_USD_PER_1K:
        pytest.skip(f"цена кэша для {model} не записана — дописать сюда при переезде")
    inp, _out = config.LLM_PRICES_USD_PER_1K[model]
    expected = CACHED_INPUT_PRICE_USD_PER_1K[model] / inp
    assert pytest.approx(expected, abs=0.005) == config.CACHED_INPUT_PRICE_MULTIPLIER
