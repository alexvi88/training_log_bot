"""progress_ui: анимированный экран прогресса (фейковый процент + чеклист этапов),
используемый под сборкой программы (см. handlers/ai_trainer._finish_setup).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

import progress_ui

STAGES = [
    "Посмотрел твою историю",
    "Подобрал упражнения под цель",
    "Расставляю веса и повторы",
    "Проверяю всё в последний раз",
]


def _message():
    message = MagicMock()
    message.edit_text = AsyncMock()
    return message


# ---------- render / initial_text ----------


def test_render_marks_done_active_and_pending_stages_by_percent():
    # 47%: первые два этапа из четырёх позади (пороги 23.25/46.5/69.75/93 при
    # cap=93), третий — в работе, четвёртый ещё не начат. Ровно то, что описано
    # в задаче как эталонный экран.
    text = progress_ui.render(47, STAGES, progress_ui._stage_done_count(47, len(STAGES), 93))

    assert "— 47%" in text
    assert text.count("✅") == 2
    assert "✅ Посмотрел твою историю" in text
    assert "✅ Подобрал упражнения под цель" in text
    assert "⏳ Расставляю веса и повторы…" in text
    assert "▫️ Проверяю всё в последний раз" in text


def test_render_does_not_double_the_ellipsis():
    text = progress_ui.render(10, ["Уже с многоточием…"], 0)
    assert "…" in text
    assert "……" not in text


def test_render_at_100_percent_checks_every_stage():
    text = progress_ui.render(100, STAGES, len(STAGES))
    assert text.count("✅") == len(STAGES)
    assert "⏳" not in text
    assert "▫️" not in text


def test_initial_text_is_zero_percent_with_first_stage_in_progress():
    text = progress_ui.initial_text(STAGES)
    assert "— 0%" in text
    assert text.count("✅") == 0
    assert f"⏳ {STAGES[0]}…" in text


# ---------- run_progress: тик, финиш, ошибка ----------


@pytest.mark.asyncio
async def test_run_progress_ticks_then_jumps_to_100_on_success():
    message = _message()

    async def slow_ok():
        await asyncio.sleep(0.05)
        return "готово"

    task = asyncio.create_task(slow_ok())
    await progress_ui.run_progress(message, task, STAGES, edit_interval=0.01)

    assert task.result() == "готово"
    message.edit_text.assert_awaited()
    texts = [call.args[0] for call in message.edit_text.await_args_list]
    # Хотя бы один промежуточный тик и обязательный финальный прыжок на 100%.
    assert any("100%" in t for t in texts)
    assert texts[-1] == progress_ui.render(100, STAGES, len(STAGES))
    # Не чаще чем раз в edit_interval — 50мс ожидания и тик в 10мс не должны
    # были высыпать в Telegram больше пары десятков правок.
    assert len(texts) < 20


@pytest.mark.asyncio
async def test_run_progress_does_not_stretch_a_fast_answer():
    """Если ответ пришёл быстрее анимации — сразу 100%, без промежуточных тиков."""
    message = _message()
    task = asyncio.create_task(asyncio.sleep(0))  # завершается почти мгновенно

    await progress_ui.run_progress(message, task, STAGES, edit_interval=1.0)

    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == progress_ui.render(100, STAGES, len(STAGES))


@pytest.mark.asyncio
async def test_run_progress_stops_without_100_percent_jump_on_failure():
    """Провал вызова — никакого зависшего чек-листа и никакого лживого 100%:
    вызывающий код сам решает, что показать (текст ошибки/лимита)."""
    message = _message()

    async def boom():
        raise RuntimeError("xai exploded")

    task = asyncio.create_task(boom())
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(asyncio.shield(task), timeout=1)
    await asyncio.sleep(0)

    await progress_ui.run_progress(message, task, STAGES, edit_interval=0.01)

    for call in message.edit_text.await_args_list:
        assert "100%" not in call.args[0]
    assert task.exception() is not None


@pytest.mark.asyncio
async def test_run_progress_stops_immediately_when_task_is_cancelled():
    message = _message()
    task = asyncio.create_task(asyncio.sleep(10))
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()

    await progress_ui.run_progress(message, task, STAGES, edit_interval=0.01)

    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_progress_suppresses_telegram_bad_request_on_every_edit():
    """«message is not modified» и протухшее сообщение не должны ронять сценарий."""
    message = _message()
    message.edit_text.side_effect = TelegramBadRequest(method=MagicMock(), message="message is not modified")

    async def slow_ok():
        await asyncio.sleep(0.03)
        return "готово"

    task = asyncio.create_task(slow_ok())
    # Не должно поднять исключение наружу, несмотря на то что каждый edit_text падает.
    await progress_ui.run_progress(message, task, STAGES, edit_interval=0.01)

    assert task.result() == "готово"


@pytest.mark.asyncio
async def test_run_progress_does_nothing_without_stages():
    message = _message()
    task = asyncio.create_task(asyncio.sleep(0))
    await progress_ui.run_progress(message, task, [], edit_interval=0.01)
    message.edit_text.assert_not_awaited()


# ---------- темп: галочки не должны кончаться раньше ожидания ----------


def _percent_at(seconds: float) -> int:
    import math

    return min(
        progress_ui.CAP_PERCENT,
        int(progress_ui.CAP_PERCENT * (1 - math.exp(-seconds / progress_ui.GROWTH_TAU))),
    )


def _stages_done_at(seconds: float, stages) -> int:
    return progress_ui._stage_done_count(_percent_at(seconds), len(stages), progress_ui.CAP_PERCENT)


def test_the_checklist_keeps_moving_through_the_whole_wait():
    """Живой репорт: экран замирал на «93%» с последним пунктом в работе, а
    ждать оставалось ещё полминуты. Четыре галочки при tau=5 проставлялись за
    первые шесть секунд — движение кончалось ровно тогда, когда оно нужнее
    всего. Сборка программы идёт десятки секунд (потолок запроса — 90), значит
    и галочки должны идти всё это время."""
    from handlers.ai_trainer import PROGRAM_PROGRESS_STAGES as stages

    # За первые пять секунд — не больше трети чек-листа.
    assert _stages_done_at(5, stages) <= len(stages) // 3
    # К десятой и к тридцатой секунде экран всё ещё двигается.
    assert _stages_done_at(10, stages) < _stages_done_at(30, stages)
    # И даже на тридцатой секунде остаётся что показать.
    assert _stages_done_at(30, stages) < len(stages)


def test_the_last_stage_never_ticks_itself():
    """Последняя галочка — это и есть настоящее «готово»: её ставит только
    run_progress по факту завершения задачи, а не таймер. Проверяем по всей
    длине ожидания, какая вообще возможна: дольше потолка запроса вызов не
    живёт (config.AI_REQUEST_TIMEOUT_SECONDS)."""
    import config
    from handlers.ai_trainer import PROGRAM_PROGRESS_STAGES as stages

    assert _stages_done_at(config.AI_REQUEST_TIMEOUT_SECONDS, stages) == len(stages) - 1
