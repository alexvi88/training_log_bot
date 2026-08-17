"""Дневные потолки AI: деньги, личные квоты и режим предупреждений для своих.

Главное, что тут стережётся, — порядок и область действия. Потолок по деньгам
обязан выключать дорогие шаги РАНЬШЕ, чем упрётся личная квота (иначе он держит
только самых активных, то есть никого), а жёсткий стоп обязан держать всех,
включая свои аккаунты, — иначе стоп-краном он не является.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import ai_limits
import config
import db as dbmod
import i18n
import i18n_coverage as i18n_coverage_module

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_cached_spend():
    """Сумма за сутки кэшируется на минуту — между тестами её надо забывать."""
    ai_limits.reset_cache()
    yield
    ai_limits.reset_cache()


@pytest.fixture
def free(monkeypatch):
    """Никаких потолков: сутки дешёвые, свои аккаунты не заданы."""
    monkeypatch.setattr(ai_limits, "daily_spend_usd", AsyncMock(return_value=0.0))
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "TEST_USER_ID", None)


def _spend(monkeypatch, amount: float) -> None:
    monkeypatch.setattr(ai_limits, "daily_spend_usd", AsyncMock(return_value=amount))


# ---------- расход за сутки ----------


async def test_cost_total_counts_tokens_flat_rates_and_tools(user_id):
    """Сумма собирается по тем же ставкам, что и строка в логе: токены по модели,
    расшифровки и вызовы инструментов — по плоской цене за вызов."""
    await dbmod.log_cost_event(
        user_id, "llm_call", model="grok-4.5-latest",
        prompt_tokens=10_000, completion_tokens=1_000,
    )
    await dbmod.log_cost_event(user_id, "transcription", model="whisper")
    await dbmod.log_cost_event(user_id, "server_tool", model="web_search")

    total = await dbmod.get_cost_total_usd(dbmod._utc_day())
    expected = (
        config.call_price_usd("grok-4.5-latest", 10_000, 1_000)
        + config.TRANSCRIPTION_PRICE_USD_PER_CALL
        + config.SERVER_TOOL_PRICE_USD_PER_CALL
    )
    assert total == pytest.approx(expected)


async def test_cost_total_prices_cached_input_cheaper(user_id):
    """Кэшированный вход в 6.7 раза дешевле обычного — потолок обязан это видеть,
    иначе тёплые сутки выглядят дороже холодных."""
    await dbmod.log_cost_event(
        user_id, "llm_call", model="grok-4.5-latest",
        prompt_tokens=30_000, completion_tokens=500, cached_tokens=29_000,
    )
    warm = await dbmod.get_cost_total_usd(dbmod._utc_day())

    await dbmod.log_cost_event(
        user_id, "llm_call", model="grok-4.5-latest",
        prompt_tokens=30_000, completion_tokens=500,
    )
    both = await dbmod.get_cost_total_usd(dbmod._utc_day())
    assert (both - warm) > warm  # холодный запрос дороже тёплого того же размера


async def test_spend_is_cached_between_calls(user_id, monkeypatch):
    counter = AsyncMock(return_value=3.0)
    monkeypatch.setattr(dbmod, "get_cost_total_usd", counter)
    assert await ai_limits.daily_spend_usd() == 3.0
    assert await ai_limits.daily_spend_usd() == 3.0
    counter.assert_awaited_once()


async def test_broken_cost_query_lets_people_through(monkeypatch):
    """Сломанный счёт не должен выключать тренера всем сразу."""
    monkeypatch.setattr(dbmod, "get_cost_total_usd", AsyncMock(side_effect=RuntimeError("нет базы")))
    assert await ai_limits.daily_spend_usd() == 0.0
    assert await ai_limits.spend_level() is None


async def test_zero_cap_disables_the_step(monkeypatch):
    _spend(monkeypatch, 1000.0)
    monkeypatch.setattr(config, "AI_DAILY_COST_SOFT_CAP_USD", 0.0)
    monkeypatch.setattr(config, "AI_DAILY_COST_HARD_STOP_USD", 0.0)
    assert await ai_limits.spend_level() is None


# ---------- потолок по деньгам ----------


async def test_soft_cap_turns_off_extras_but_not_the_trainer(user_id, monkeypatch, free):
    _spend(monkeypatch, config.AI_DAILY_COST_SOFT_CAP_USD + 0.01)

    for kind in (ai_limits.KIND_SEARCH, ai_limits.KIND_VIDEO, ai_limits.KIND_FOOD):
        block = await ai_limits.check(user_id, kind)
        assert block is not None, kind
        assert block.kind == kind
    # Вопрос — это и есть продукт: на первом же дорогом дне его не рубим.
    assert await ai_limits.check(user_id, ai_limits.KIND_QUESTION) is None


async def test_soft_cap_blocks_before_personal_quota_is_spent(user_id, monkeypatch, free):
    """Потолок обязан сработать у человека, который сегодня не потратил ничего:
    иначе он держит только самых активных."""
    _spend(monkeypatch, config.AI_DAILY_COST_SOFT_CAP_USD)
    assert await dbmod.get_ai_video_count_today(user_id) == 0

    block = await ai_limits.check(user_id, ai_limits.KIND_VIDEO)
    assert block is not None
    assert "spend_soft" in block.log


async def test_hard_stop_silences_the_trainer(user_id, monkeypatch, free):
    _spend(monkeypatch, config.AI_DAILY_COST_HARD_STOP_USD)

    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    assert block is not None
    assert block.kind == ai_limits.KIND_SPEND_HARD
    assert "завтра" in block.user_text


async def test_hard_stop_holds_own_accounts_too(user_id, monkeypatch):
    """Единственный лимит без поблажки своим: стоп-кран, который можно проехать,
    стоп-краном не является."""
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    _spend(monkeypatch, config.AI_DAILY_COST_HARD_STOP_USD)

    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    assert block is not None
    assert not block.preview


async def test_hard_stop_block_is_none_below_the_hard_cap(user_id, monkeypatch, free):
    """Ниже HARD платные входы без своей квоты (кнопка комментария и т.п.) не
    должны спотыкаться о деньги вовсе."""
    assert await ai_limits.hard_stop_block() is None


async def test_hard_stop_block_fires_on_the_hard_cap(user_id, monkeypatch, free):
    _spend(monkeypatch, config.AI_DAILY_COST_HARD_STOP_USD)

    block = await ai_limits.hard_stop_block()
    assert block is not None
    assert block.kind == ai_limits.KIND_SPEND_HARD
    assert "завтра" in block.user_text


async def test_hard_stop_block_ignores_the_soft_cap(user_id, monkeypatch, free):
    """SOFT выключает только «лишние» дорогие шаги (_EXTRAS) — вход без личной
    квоты этим списком не покрыт, так что SOFT его не трогает вовсе."""
    _spend(monkeypatch, config.AI_DAILY_COST_SOFT_CAP_USD)

    assert await ai_limits.hard_stop_block() is None


# ---------- личные квоты ----------


async def test_question_quota_blocks_at_the_limit(user_id, monkeypatch, free):
    for _ in range(config.AI_QUESTION_DAILY_LIMIT):
        await dbmod.increment_ai_question_count(user_id)

    block = await ai_limits.check(user_id, ai_limits.KIND_QUESTION)
    assert block is not None
    assert str(config.AI_QUESTION_DAILY_LIMIT) in block.log


async def test_food_quota_counts_and_blocks(user_id, monkeypatch, free):
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 2)
    assert await ai_limits.check(user_id, ai_limits.KIND_FOOD) is None

    await dbmod.increment_ai_food_count(user_id)
    await dbmod.increment_ai_food_count(user_id)
    assert await dbmod.get_ai_food_count_today(user_id) == 2

    block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
    assert block is not None
    # Тупика нет: человеку сказано, что делать дальше (TONE_OF_VOICE.md).
    assert "словами" in block.user_text


async def test_search_block_stays_silent_for_the_athlete(user_id, monkeypatch, free):
    """Человек не просил лезть в сеть — он и не должен читать про отменённый шаг."""
    monkeypatch.setattr(config, "AI_SEARCH_DAILY_LIMIT", 1)
    await dbmod.increment_ai_search_count(user_id)

    block = await ai_limits.check(user_id, ai_limits.KIND_SEARCH)
    assert block is not None
    assert block.user_text is None


async def test_global_search_cap_is_shared_between_people(user_id, monkeypatch, free):
    """Личная квота умножается на число людей, общая — нет.

    Общий счётчик теперь резервируется атомарно и ДО сетевого похода (см.
    db.try_increment_ai_search_count_global и ai_trainer._search_block), а не
    одной транзакцией вместе с личным — поэтому сеется он тут напрямую.
    """
    monkeypatch.setattr(config, "AI_SEARCH_GLOBAL_DAILY_LIMIT", 1)
    await dbmod.try_increment_ai_search_count_global(config.AI_SEARCH_GLOBAL_DAILY_LIMIT)

    stranger = 999_111
    assert await ai_limits.check(stranger, ai_limits.KIND_SEARCH) is None
    assert await ai_limits.check(stranger, ai_limits.KIND_SEARCH_GLOBAL) is not None


# ---------- предупреждения на своих аккаунтах ----------


async def test_own_account_sees_a_warning_instead_of_a_wall(user_id, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    _spend(monkeypatch, 0.0)
    await dbmod.increment_ai_food_count(user_id)

    block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
    assert block is not None and block.preview
    # В предупреждении — ровно тот текст, который увидел бы обычный атлет.
    assert block.user_text in ai_limits.preview_text(block, 1.23)
    assert "$1.23" in ai_limits.preview_text(block, 1.23)


async def test_own_account_preview_follows_the_acting_users_language(user_id, monkeypatch):
    """QA-баг: предупреждение своим аккаунтам рендерилось жёстко по-русски,
    даже когда язык атлета (в т.ч. TEST_USER_ID/ADMIN_ID) — английский, хотя
    оно и задумано как симуляция того, что увидел бы обычный атлет (CLAUDE.md,
    «Сколько это стоит»)."""
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    _spend(monkeypatch, 1.23)
    await dbmod.increment_ai_food_count(user_id)

    # check() и последующий показ предупреждения всегда идут в рамках ОДНОГО
    # запроса, на языке которого и застывает block.user_text (см. ai_limits.py:
    # `_user_text` через i18n.t) — как в проде, где язык уже выставлен
    # мидлварью до хендлера. Поэтому блок пересоздаётся под каждый язык
    # отдельно, а не переиспользуется между `use_lang`.
    with i18n.use_lang("ru"):
        ru_block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
        assert ru_block is not None and ru_block.preview
        ru_text = ai_limits.preview_text(ru_block, 1.23)
    with i18n.use_lang("en"):
        en_block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
        assert en_block is not None and en_block.preview
        en_text = ai_limits.preview_text(en_block, 1.23)

    assert ru_text != en_text
    assert "СРАБОТАЛ ЛИМИТ" in ru_text and "Обычный атлет" in ru_text
    assert "LIMIT HIT" in en_text and "average athlete" in en_text
    # Никакой кириллицы не протекает в английскую версию экрана.
    assert not i18n_coverage_module.has_cyrillic(en_text)


async def test_ack_lets_the_day_continue(user_id, monkeypatch):
    monkeypatch.setattr(config, "TEST_USER_ID", user_id)
    monkeypatch.setattr(config, "ADMIN_ID", None)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    _spend(monkeypatch, 0.0)
    await dbmod.increment_ai_food_count(user_id)

    assert (await ai_limits.check(user_id, ai_limits.KIND_FOOD)).preview
    await ai_limits.record_ack(user_id, ai_limits.KIND_FOOD)
    assert await ai_limits.check(user_id, ai_limits.KIND_FOOD) is None


async def test_ack_covers_only_its_own_kind(user_id, monkeypatch):
    """Нажал «Понятно» на еде — про видео это ничего не говорит."""
    monkeypatch.setattr(config, "ADMIN_ID", user_id)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    monkeypatch.setattr(config, "AI_VIDEO_DAILY_LIMIT", 1)
    _spend(monkeypatch, 0.0)
    await dbmod.increment_ai_food_count(user_id)
    await dbmod.increment_ai_video_count(user_id)

    await ai_limits.record_ack(user_id, ai_limits.KIND_FOOD)
    assert await ai_limits.check(user_id, ai_limits.KIND_FOOD) is None
    assert (await ai_limits.check(user_id, ai_limits.KIND_VIDEO)).preview


async def test_strangers_never_get_the_warning(user_id, monkeypatch, free):
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    await dbmod.increment_ai_food_count(user_id)

    block = await ai_limits.check(user_id, ai_limits.KIND_FOOD)
    assert block is not None and not block.preview


async def test_ack_confirmation_follows_the_acting_users_language(user_id, monkeypatch):
    """QA-баг, вторая половина: строка, которой хендлер дописывает сообщение
    после тапа «Понятно» (handlers/admin.limit_ack), тоже была жёстко
    по-русски — тот же класс бага, что и в preview_text."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from handlers import admin as admin_handlers

    monkeypatch.setattr(config, "ADMIN_ID", user_id)

    message = MagicMock()
    message.text = "some limit warning"
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = f"ail:ack:{ai_limits.KIND_FOOD}"
    callback.answer = AsyncMock()

    with i18n.use_lang("en"):
        await admin_handlers.limit_ack(callback)

    appended = message.edit_text.await_args.args[0]
    assert "Got it" in appended
    assert not i18n_coverage_module.has_cyrillic(appended)
    toast = callback.answer.await_args.args[0]
    assert not i18n_coverage_module.has_cyrillic(toast)


# ---------- атомарный check-and-increment (гонка «прочитал → подождал → списал») ----------
#
# Раньше решение «можно» принималось чтением счётчика, а списание —
# отдельным запросом секундами позже, после ответа модели. Между ними была
# гонка: два параллельных запроса читали один и тот же счётчик до того, как
# любой из них его увеличил, и оба проходили. `try_increment_ai_*` сводит
# проверку и запись в одно атомарное `UPDATE ... WHERE count < limit`.


async def test_try_increment_question_count_stops_exactly_at_the_limit(user_id):
    for _ in range(3):
        assert await dbmod.try_increment_ai_question_count(user_id, limit=3) is True
    # Четвёртая попытка — гонка (или просто перебор): счётчик уже на потолке,
    # и rowcount у UPDATE обязан быть 0, а не молча перескочить дальше.
    assert await dbmod.try_increment_ai_question_count(user_id, limit=3) is False
    assert await dbmod.get_ai_question_count_today(user_id) == 3


async def test_try_increment_question_count_unlimited_when_limit_is_zero_or_less(fresh_db):
    for limit in (0, -1):
        user_id = 555_000 + limit
        for _ in range(5):
            assert await dbmod.try_increment_ai_question_count(user_id, limit=limit) is True
        assert await dbmod.get_ai_question_count_today(user_id) == 5


async def test_try_increment_search_global_stops_exactly_at_the_limit(fresh_db):
    for _ in range(2):
        assert await dbmod.try_increment_ai_search_count_global(limit=2) is True
    assert await dbmod.try_increment_ai_search_count_global(limit=2) is False
    assert await dbmod.get_ai_search_count_global() == 2


async def test_try_increment_search_global_unlimited_when_limit_is_zero_or_less(fresh_db):
    for _ in range(4):
        assert await dbmod.try_increment_ai_search_count_global(limit=0) is True
    assert await dbmod.get_ai_search_count_global() == 4


async def test_try_increment_search_global_survives_a_burst_of_concurrent_callers(fresh_db):
    """То самое: N «параллельных» вызовов (тут — конкурентных корутин на одном
    event loop, что и создавало реальную гонку в проде между чтением и
    записью) не должны провести общий счётчик дальше потолка."""
    results = await asyncio.gather(
        *(dbmod.try_increment_ai_search_count_global(limit=5) for _ in range(20))
    )
    assert sum(results) == 5
    assert await dbmod.get_ai_search_count_global() == 5


async def test_ack_lives_by_the_same_clock_as_its_limit(user_id, monkeypatch):
    """Личные квоты живут по суткам пользователя, общие и деньги — по UTC.
    Расписка обязана гаснуть вместе со своим лимитом, а не раньше или позже."""
    monkeypatch.setattr(dbmod, "_quota_day", AsyncMock(return_value="2026-08-09"))
    monkeypatch.setattr(dbmod, "_utc_day", lambda: "2026-08-10")

    assert await ai_limits.ack_day(user_id, ai_limits.KIND_FOOD) == "2026-08-09"
    assert await ai_limits.ack_day(user_id, ai_limits.KIND_SEARCH_GLOBAL) == "2026-08-10"
    assert await ai_limits.ack_day(user_id, ai_limits.KIND_SPEND_SOFT) == "2026-08-10"
