"""Дневные потолки AI: одна точка, где решается «этот дорогой шаг делаем или нет».

Раньше решение было размазано по местам вызова: квота вопросов проверялась в
хендлере, поисковая — внутри `ai_trainer.ask`, видео — в третьем месте, а
дневник питания не проверялся вовсе. Пока лимит был один на всех и один вид, это
работало; как только их стало пять, разъезжаться начало предсказуемо — новый
платный вызов просто забывали накрыть.

Что здесь решается:

1. **Личные квоты** — сколько вопросов, поисков, видео и разборов еды в сутки
   можно одному человеку (config.AI_*_DAILY_LIMIT). Считаются по календарным
   суткам ПОЛЬЗОВАТЕЛЯ (db._quota_day): квота, которая обнуляется посреди
   вечерней тренировки, выглядит произвольной.

2. **Общий потолок поисков** — тот же счёт, но на всех сразу, по UTC.

3. **Потолок по деньгам** — две ступени, SOFT и HARD (см. config). Личные квоты
   умножаются на число пришедших, этот — нет. Считает доллары, а не запросы:
   цена вопроса гуляет от $0.015 до $0.23 в зависимости от кэша и живого поиска
   (LLM_COSTS.md), так что число запросов о расходе не говорит почти ничего.

4. **Режим предупреждений для своих** (config.limit_preview_ids) — на своих
   аккаунтах лимит первый раз за сутки показывается предупреждением с кнопкой
   «Понятно» и текстом, который в этот момент увидел бы обычный атлет; после
   нажатия этот вид лимита до конца суток пропускает. Так видно, КОГДА обычный
   человек впервые упёрся, и при этом бота можно смотреть дальше в тот же день.
   Единственное исключение — HARD-стоп по деньгам: он держит всех.

Точка входа одна — `check(user_id, kind)`. Вернула None — шаг разрешён.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import billing
import config
import db
import keyboards

logger = logging.getLogger(__name__)

KIND_QUESTION = "question"
KIND_SEARCH = "search"
KIND_SEARCH_GLOBAL = "search_global"
KIND_VIDEO = "video"
KIND_FOOD = "food"
# Не суточный потолок, а деньги: бесплатные вопросы месяца кончились (billing.py).
# Отдельный вид, потому что и лечится он иначе — не «приходи завтра», а витрина.
KIND_QUESTION_MONTH = "question_month"
KIND_SPEND_SOFT = "spend_soft"
KIND_SPEND_HARD = "spend_hard"

# Дорогие необязательные шаги: их SOFT-потолок выключает первыми, оставляя
# тренера на связи. Вопрос сюда не входит намеренно — он и есть продукт.
_EXTRAS = (KIND_SEARCH, KIND_SEARCH_GLOBAL, KIND_VIDEO, KIND_FOOD)


@dataclass
class Block:
    """Почему шаг не состоялся — и что показать человеку.

    kind — вид лимита, он же ключ расписки «Понятно» (db.ai_limit_ack).
    log — строка для лога и для поля search_outcome, всегда есть.
    user_text — что показать атлету. None у поисковых: человек не просил
        лезть в сеть и не должен читать про отменённый шаг, он просто получает
        ответ без свежести.
    preview — это свой аккаунт, и предупреждение показывается ВМЕСТО отказа:
        текущий шаг всё равно не делается, но после «Понятно» следующие пройдут.
    """

    kind: str
    log: str
    user_text: Optional[str] = None
    preview: bool = False
    # Клавиатура, которую этот отказ приносит с собой, — сейчас только у
    # пейволла: витрина без кнопки «купить» осталась бы фразой без выхода.
    # None — берём ту, что дал вызывающий (обычно клавиатура чата тренера).
    markup: Optional[object] = None


# --- расход за сутки -------------------------------------------------------
#
# Сумма считается агрегатом по cost_events и нужна перед каждым дорогим шагом,
# поэтому держим её недолгим кэшем (config.AI_COST_CACHE_SECONDS). Плата за кэш —
# «перелёт» потолка на минуту расходов; при нашей скорости это центы, а без кэша
# каждый вопрос тащил бы за собой лишний запрос к базе.
_cached: Optional[tuple[str, float, float]] = None  # (сутки UTC, сумма, monotonic)


def reset_cache() -> None:
    """Забыть посчитанное — для тестов и для ручного «пересчитай сейчас»."""
    global _cached
    _cached = None


async def daily_spend_usd() -> float:
    """Сколько денег стоили текущие сутки. Не смогли посчитать — считаем, что ноль.

    Пропускать вперёд при сломанном счёте — сознательный выбор. Потолок здесь
    страхует от разорения, а не охраняет вход: развернись эта ошибка в отказ, и
    любой сбой базы выключал бы тренера всем сразу, молча и до починки. Обратная
    цена — сутки без страховки — видна в логе и в ночном отчёте.

    Не кэшируем ноль, полученный из ошибки: иначе одна неудачная попытка гасила
    бы потолок на всю минуту кэша.
    """
    global _cached
    today = db._utc_day()
    if _cached is not None:
        day, spend, at = _cached
        if day == today and time.monotonic() - at < config.AI_COST_CACHE_SECONDS:
            return spend
    try:
        spend = await db.get_cost_total_usd(today)
    except Exception:
        logger.exception("не смог посчитать расход за сутки — потолок сегодня не держит")
        return 0.0
    _cached = (today, spend, time.monotonic())
    return spend


async def spend_level() -> Optional[str]:
    """None / KIND_SPEND_SOFT / KIND_SPEND_HARD — до какой ступени доехали сутки.

    Ноль или отрицательное значение потолка выключает ступень: снять потолок на
    время должно быть можно переменной окружения, не выкатывая код.
    """
    spend = await daily_spend_usd()
    hard = config.AI_DAILY_COST_HARD_STOP_USD
    soft = config.AI_DAILY_COST_SOFT_CAP_USD
    if hard > 0 and spend >= hard:
        return KIND_SPEND_HARD
    if soft > 0 and spend >= soft:
        return KIND_SPEND_SOFT
    return None


# --- тексты ----------------------------------------------------------------
#
# Голосом тренера (TONE_OF_VOICE.md): что случилось и что делать дальше. Про
# доллары, потолки и модели атлет не читает — это наша кухня, а не его дело.

_HARD_STOP_TEXT = (
    "Сегодня я уже отговорил своё — вернусь завтра. "
    "Дневник, программы и история работают как обычно."
)

QUESTION_LIMIT_TEXT = (
    "На сегодня лимит вопросов исчерпан 😮‍💨 Дай тренеру передохнуть — возвращайся завтра."
)

# Бесплатные вопросы месяца кончились. Не подколка и не упрёк: подкалываем
# только за пропуски, а этот человек, наоборот, ходил к тренеру так часто, что
# упёрся в потолок. Поэтому — что кончилось, что остаётся бесплатным навсегда и
# два выхода: подождать сброса или забрать платное.
MONTH_LIMIT_TEXT = (
    "Бесплатные вопросы на этот месяц кончились — их {free} 😮‍💨\n\n"
    "Дневник, история, графики и программы при тебе: их не трону никогда. "
    "Первого числа счётчик обнулю, и спрашивай дальше.\n\n"
    "Ждать не хочешь — забери платный доступ, там тренер без счётчика."
)

# То же самое тому, кому платное ещё не показываем (меньше
# config.PAYWALL_MIN_WORKOUTS закрытых тренировок): витрина ему сейчас
# прочиталась бы как «бот оказался платным», поэтому только про сброс счётчика.
MONTH_LIMIT_NO_OFFER_TEXT = (
    "Бесплатные вопросы на этот месяц кончились — их {free} 😮‍💨\n\n"
    "Дневник, история и графики при тебе. Первого числа счётчик обнулю — "
    "возвращайся с вопросами."
)


def _video_text(reason: str, limit: int = 0) -> str:
    if reason == KIND_VIDEO:
        return (
            f"На сегодня разобрал {limit or config.AI_VIDEO_DAILY_LIMIT} видео — это лимит. "
            "Приходи завтра, а пока спрашивай текстом."
        )
    return "Видео сегодня больше не разбираю — вернусь к этому завтра. Спрашивай текстом, отвечу как обычно."


def _food_text(reason: str) -> str:
    if reason == KIND_FOOD:
        return (
            f"На сегодня разобрал {config.AI_FOOD_DAILY_LIMIT} приёмов пищи — это лимит. "
            "Напиши словами, что съел, — запишу как есть, а считать буду завтра."
        )
    return (
        "Фото сегодня больше не разбираю — вернусь к этому завтра. "
        "Напиши словами, что съел, — запишу как есть."
    )


def _user_text(kind: str, reason: str, limit: int = 0) -> Optional[str]:
    """reason — из-за чего блок: сам вид лимита или ступень по деньгам.

    limit — сколько этому человеку на самом деле положено (у оплаченного
    доступа потолок видео выше общего): называть в отказе чужое число значит
    спорить с тем, что он только что видел.
    """
    if reason == KIND_SPEND_HARD:
        return _HARD_STOP_TEXT
    if kind == KIND_QUESTION:
        return QUESTION_LIMIT_TEXT
    if kind == KIND_VIDEO:
        return _video_text(reason, limit)
    if kind == KIND_FOOD:
        return _food_text(reason)
    # Поисковые: молча отвечаем без свежести.
    return None


_KIND_TITLES = {
    KIND_QUESTION: "вопросы тренеру",
    KIND_SEARCH: "живой поиск, личная квота",
    KIND_SEARCH_GLOBAL: "живой поиск, общий потолок",
    KIND_VIDEO: "разбор видео",
    KIND_FOOD: "разбор еды",
    KIND_QUESTION_MONTH: "бесплатные вопросы месяца",
    KIND_SPEND_SOFT: "расход за сутки",
    KIND_SPEND_HARD: "расход за сутки, стоп",
}


def preview_text(block: Block, spend: float) -> str:
    """Что видит свой аккаунт вместо отказа.

    Капс — заголовком, как и положено (TONE_OF_VOICE.md): дальше идёт проза.
    Показываем ровно ту фразу, которую в эту секунду прочитал бы обычный
    атлет, — ради этого всё и затевалось: понять, КОГДА и КАКИМ текстом лимит
    встречает человека из поста.
    """
    lines = [
        f"⚠️ СРАБОТАЛ ЛИМИТ: {_KIND_TITLES.get(block.kind, block.kind)}",
        "",
        f"За сутки набежало ~${spend:.2f}.",
    ]
    if block.user_text:
        lines += ["", "Обычный атлет сейчас увидел бы:", f"«{block.user_text}»"]
    else:
        lines += ["", "Обычный атлет ничего не увидел бы — ответ просто идёт без свежести."]
    lines += ["", "Жми «Понятно» — до конца суток этот лимит тебя пропускает."]
    return "\n".join(lines)


# --- собственно решение ----------------------------------------------------


async def _question_block(user_id: int) -> Optional[Block]:
    """Вопрос тренеру: дневная квота и месячный пейволл одной проверкой.

    Считает не сам, а спрашивает billing.allowance — там же живут оплаченный
    доступ и разовые паки, и раздваивать этот счёт нельзя: разъедься он, и
    человек либо платит зря, либо спрашивает бесплатно сверх лимита.

    Дневной и месячный разводятся намеренно. «Приходи завтра» человеку, у
    которого кончился месяц, — враньё: завтра не изменится ничего, изменится
    первого числа. Отсюда и разные тексты, и разный выход.
    """
    allow = await billing.allowance(user_id)
    if allow.allowed:
        return None
    if allow.blocked_by != "month":
        return Block(
            kind=KIND_QUESTION,
            log=f"{KIND_QUESTION}: {allow.day_limit} из {allow.day_limit} за сутки",
            user_text=QUESTION_LIMIT_TEXT,
        )
    free = config.AI_QUESTION_MONTHLY_FREE
    if not await billing.may_offer(user_id):
        return Block(
            kind=KIND_QUESTION_MONTH,
            log=f"{KIND_QUESTION_MONTH}: бесплатные {free} за месяц выбраны, витрину не показываем",
            user_text=MONTH_LIMIT_NO_OFFER_TEXT.format(free=free),
        )
    return Block(
        kind=KIND_QUESTION_MONTH,
        log=f"{KIND_QUESTION_MONTH}: бесплатные {free} за месяц выбраны",
        user_text=MONTH_LIMIT_TEXT.format(free=free),
        markup=keyboards.billing_paywall(),
    )


async def _exhausted(user_id: int, kind: str) -> Optional[str]:
    """Личная (или общая) квота этого вида кончилась? Вернёт строку для лога.

    Вопросы сюда не попадают — у них своя ветка (_question_block): там кроме
    суточной квоты есть ещё деньги.
    """
    if kind == KIND_SEARCH:
        used = await db.get_ai_search_count_today(user_id)
        limit = config.AI_SEARCH_DAILY_LIMIT
    elif kind == KIND_SEARCH_GLOBAL:
        used = await db.get_ai_search_count_global()
        limit = config.AI_SEARCH_GLOBAL_DAILY_LIMIT
    elif kind == KIND_VIDEO:
        used = await db.get_ai_video_count_today(user_id)
        limit = await billing.video_daily_limit(user_id)
    elif kind == KIND_FOOD:
        used = await db.get_ai_food_count_today(user_id)
        limit = config.AI_FOOD_DAILY_LIMIT
    else:
        return None
    if limit > 0 and used >= limit:
        return f"{kind}: {used} из {limit} за сутки"
    return None


async def ack_day(user_id: int, kind: str) -> str:
    """Сутки, за которые действует расписка «Понятно».

    Общий потолок поисков и деньги живут по UTC (счёт от провайдера), остальное
    — по календарным суткам пользователя. Расписка обязана жить по тем же часам,
    что и лимит, иначе она погаснет раньше или позже него.
    """
    if kind in (KIND_SEARCH_GLOBAL, KIND_SPEND_SOFT, KIND_SPEND_HARD):
        return db._utc_day()
    return await db._quota_day(user_id)


async def check(user_id: int, kind: str) -> Optional[Block]:
    """Делаем этот шаг или нет. None — делаем.

    Порядок важен: деньги смотрим раньше личной квоты. Человек, у которого своя
    квота ещё не выбрана, всё равно не должен запускать дорогой шаг в сутки,
    когда общий счёт уже перевалил за потолок, — иначе потолок держит только
    самых активных, то есть никого.
    """
    reason = None
    level = await spend_level()
    if level == KIND_SPEND_HARD and kind == KIND_QUESTION:
        # Единственный лимит, который не пропускает никого, включая свои
        # аккаунты: стоп-кран, который можно проехать, стоп-краном не является.
        spend = await daily_spend_usd()
        logger.warning("AI hard stop: за сутки ~$%.2f, тренер молчит до полуночи UTC", spend)
        return Block(kind=KIND_SPEND_HARD, log=f"spend_hard: ~${spend:.2f}", user_text=_HARD_STOP_TEXT)
    if level is not None and kind in _EXTRAS:
        # HARD включает в себя SOFT: до вопросов дело дошло, значит и дорогие
        # шаги давно выключены.
        reason = level
    if reason is None:
        if kind == KIND_QUESTION:
            # У вопросов своя ветка: кроме суточной квоты там месячный пейволл,
            # и отказ приезжает с готовым текстом и своей клавиатурой.
            block = await _question_block(user_id)
            if block is None:
                return None
            return await _with_preview(user_id, block)
        exhausted = await _exhausted(user_id, kind)
        if exhausted is None:
            return None
        reason = kind
        log = exhausted
    else:
        log = f"{reason}: за сутки ~${await daily_spend_usd():.2f}"

    limit_hint = await billing.video_daily_limit(user_id) if kind == KIND_VIDEO else 0
    block = Block(kind=kind, log=log, user_text=_user_text(kind, reason, limit_hint))
    return await _with_preview(user_id, block)


async def _with_preview(user_id: int, block: Block) -> Optional[Block]:
    """Своим аккаунтам — предупреждение вместо отказа, но только раз в сутки.

    Расписка «Понятно» уже есть — пропускаем этот вид лимита до конца суток.
    """
    if user_id in config.limit_preview_ids():
        if await db.has_limit_ack(user_id, block.kind, await ack_day(user_id, block.kind)):
            logger.info("limit %s пропущен: свой аккаунт %s уже нажал «Понятно»", block.kind, user_id)
            return None
        block.preview = True
    return block


async def record_ack(user_id: int, kind: str) -> None:
    await db.record_limit_ack(user_id, kind, await ack_day(user_id, kind))


async def reply(message, block: Block, *, reply_markup=None) -> None:
    """Показать отказ (или предупреждение своим) — одинаково во всех местах.

    Хендлеры отличаются только клавиатурой под обычным отказом: из чата тренера
    без кнопок оставался бы один выход через нижнее меню. У предупреждения
    клавиатура своя — кнопка «Понятно».
    """
    if block.preview:
        await message.reply(
            preview_text(block, await daily_spend_usd()),
            reply_markup=keyboards.limit_ack_keyboard(block.kind),
        )
        return
    if block.user_text:
        # Своя клавиатура отказа важнее переданной: у пейволла это кнопка на
        # витрину, и подменять её клавиатурой чата значит оставить предложение
        # без единого выхода.
        await message.reply(block.user_text, reply_markup=block.markup or reply_markup)
