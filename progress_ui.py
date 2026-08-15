"""Анимированный экран прогресса: фейковый процент + чеклист этапов.

Пока идёт долгий вызов модели (сборка программы — сначала, но задумано и под
разбор видео/импорт позже — см. `run_progress`), человеку нужно не «тренер
думает...» на тридцать секунд, а ощущение, что дело идёт: растущий процент и
галочки по этапам, как в онбординге фитнес-приложений.

Процент тут фейковый и растёт по таймеру, а не по реальному прогрессу — узнать
реальную долю выполненной работы у одного вызова модели неоткуда (весь ответ
приезжает одним куском, см. handlers/ai_trainer._handle_question). Кривая
нелинейная — быстро в начале, всё медленнее дальше — и упирается в потолок
`CAP_PERCENT`, а не в 100: дойти до сотни раньше, чем реальный ответ готов,
значило бы соврать. Настоящее завершение (успешное) — отдельный прыжок на 100%
со всеми галочками; его показывает сам `run_progress`, как только переданная
задача завершилась без исключения. Если задача упала или её отменили —
никакого прыжка: вызывающий код сам решает, что показать вместо экрана
(текст ошибки/лимита), а эта функция просто перестаёт трогать сообщение.

Этапы возникают из тех же долей потолка, что и сам процент: имя i-го этапа
считается «пройденным», когда процент дорос до `CAP_PERCENT * (i+1) / N` — то
есть чек-лист заполняется теми же темпами, что и цифра сверху, без отдельного
таймера на каждый пункт.
"""

import asyncio
import math
import time
from contextlib import suppress
from typing import Optional, Sequence

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

# Заголовок и иконка по умолчанию — сборка программы. Другой сценарий
# (видео/импорт) передаёт свои через параметры run_progress.
DEFAULT_TITLE = "Собираю программу под тебя"
DEFAULT_ICON = "🏗"

# Потолок фейкового процента — дальше цифра не растёт, пока задача реально не
# закончится. 93, а не 99 — так виднее, что до конца ещё есть шаг (финальная
# проверка), а не «завис на 99%».
CAP_PERCENT = 93

# Постоянная времени кривой percent = CAP * (1 - e^(-t/tau)): при tau=5 к 18
# секундам (ожидаемая длина вызова, см. config.AI_REQUEST_TIMEOUT_SECONDS и
# комментарий у _finish_setup) кривая доходит примерно до 90 из 93 — заметно
# быстрее в начале и всё медленнее к потолку, как и задумано.
GROWTH_TAU = 5.0

# Реже этого сообщение не редактируем — Telegram ограничивает частоту правок
# одного сообщения, а раз в секунду человек и не заметит разницы глазами.
EDIT_INTERVAL = 2.0


def _stage_done_count(percent: float, total_stages: int, cap_percent: float) -> int:
    if total_stages <= 0:
        return 0
    done = 0
    for i in range(total_stages):
        threshold = cap_percent * (i + 1) / total_stages
        if percent >= threshold:
            done += 1
    return done


def render(
    percent: int,
    stages: Sequence[str],
    done_count: int,
    *,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
) -> str:
    """Собрать текст экрана: заголовок с процентом и чеклист этапов.

    Текущий (ещё не пройденный) этап помечается ⏳ и получает многоточие, если
    в тексте его ещё нет, — так же, как готовые фразы running_texts.py дают
    понять, что тренер не закончил и не завис.
    """
    lines = [f"{icon} {title} — {percent}%", ""]
    for i, stage in enumerate(stages):
        if i < done_count:
            lines.append(f"✅ {stage}")
        elif i == done_count:
            suffix = "" if stage.rstrip().endswith(("…", ".", "!", "?")) else "…"
            lines.append(f"⏳ {stage}{suffix}")
        else:
            lines.append(f"▫️ {stage}")
    return "\n".join(lines)


def initial_text(stages: Sequence[str], *, title: str = DEFAULT_TITLE, icon: str = DEFAULT_ICON) -> str:
    """Текст самого первого сообщения — 0%, первый этап уже «в работе»."""
    return render(0, stages, 0, title=title, icon=icon)


async def run_progress(
    message: Message,
    task: "asyncio.Task",
    stages: Sequence[str],
    *,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
    cap_percent: int = CAP_PERCENT,
    growth_tau: float = GROWTH_TAU,
    edit_interval: float = EDIT_INTERVAL,
) -> None:
    """Крутить процент+чеклист в `message`, пока не завершится `task`.

    `task` — asyncio.Task с реальным вызовом (обычно ai_trainer.ask): эта
    функция его не запускает и не трогает результат, только следит, закончился
    ли он. Вызывающий код сам делает что-то с результатом/исключением после
    возврата отсюда (или awaiting того же task — Task кэширует результат,
    повторный await безопасен).

    Ждём task через `asyncio.wait` с таймаутом `edit_interval` — это разом и
    ограничивает частоту правок (не чаще раза в edit_interval), и ловит
    досрочное завершение без лишней задержки: если ответ пришёл быстрее
    анимации, следующая же итерация видит task завершённым и сразу прыгает на
    100%, не дожидаясь очередного тика.

    Если задача упала или её отменили — никакого прыжка на 100%, функция
    просто возвращает управление: вызывающий код увидит исключение через сам
    `task` и заменит экран текстом ошибки.
    """
    if not stages:
        return
    start = time.monotonic()
    last_text: Optional[str] = None
    while not task.done():
        done, _pending = await asyncio.wait({task}, timeout=edit_interval)
        if task in done:
            break
        elapsed = time.monotonic() - start
        percent = min(cap_percent, int(cap_percent * (1 - math.exp(-elapsed / growth_tau))))
        text = render(
            percent, stages, _stage_done_count(percent, len(stages), cap_percent), title=title, icon=icon
        )
        if text != last_text:
            with suppress(TelegramBadRequest):
                await message.edit_text(text)
            last_text = text

    if task.cancelled():
        return
    if task.exception() is not None:
        return

    final_text = render(100, stages, len(stages), title=title, icon=icon)
    if final_text != last_text:
        with suppress(TelegramBadRequest):
            await message.edit_text(final_text)
