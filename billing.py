"""Монетизация: что продаём за звёзды и кто сколько вопросов ещё может задать.

Экраны и приём платежей — `handlers/billing.py`, хранение — `db.py`, экономика
и риски — `MONETIZATION.md`. Здесь правила: витрина товаров, расчёт остатка и
списание.

Что бесплатно навсегда: дневник, история, графики, программы, достижения,
экспорт и MCP. Под платой — только AI-тренер, потому что каждый его вопрос
стоит нам живых денег провайдера (см. `cost_events`, `LLM_COSTS.md`). Пейволл
на собственную историю человека мы не ставим ни при каких условиях.

Порядок трат жёсткий и всегда один: сначала бесплатные вопросы месяца, потом
разовый пак. С оплаченным доступом не тратится ничего — у него свой дневной
потолок и никакого месячного счётчика. Иначе купивший подписку в середине
месяца сжигал бы остаток бесплатных, а на следующий месяц обнаружил, что пак
растаял, пока он им не пользовался.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Optional

import config
import db

logger = logging.getLogger(__name__)


# Коды товаров едут в invoice payload и в star_payments.product — менять их
# нельзя, не сломав чтение уже совершённых платежей.
PRO_MONTH = "pro_month"
QUESTION_PACK = "question_pack"

# Сколько рублей остаётся НАМ с одной звезды после всех комиссий — нижняя
# граница из живых отчётов (MONETIZATION.md: за 1000 звёзд на руки выходит
# 650–850 ₽). Берём именно нижнюю: завышенная цифра в отчёте — это решение
# вкладываться в канал, который на самом деле не окупился.
STARS_NET_RUB_PER_STAR = 0.65


@dataclass(frozen=True)
class Product:
    """Товар витрины. `stars` и объём читаются из config на каждом обращении —
    цена меняется переменной окружения, без выката кода."""

    code: str
    title: str
    description: str

    @property
    def stars(self) -> int:
        return config.PRO_PRICE_STARS if self.code == PRO_MONTH else config.PACK_PRICE_STARS


PRODUCTS: dict[str, Product] = {
    PRO_MONTH: Product(
        code=PRO_MONTH,
        title="Тренер на месяц",
        # Описание уезжает в счёт Telegram, а там нет ни разметки, ни кнопок —
        # только этот абзац, поэтому он говорит и что даёт, и на сколько.
        description=(
            "Спрашиваешь тренера без оглядки на счётчик 30 дней. "
            "Разборы техники по видео — тоже."
        ),
    ),
    QUESTION_PACK: Product(
        code=QUESTION_PACK,
        title="Пак вопросов",
        description=(
            "Разовый запас вопросов к тренеру. Не сгорает: лежит, пока не "
            "потратишь."
        ),
    ),
}


def _parse_until(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        # Битую дату лечим как отсутствие доступа, но громко: молча отобранный
        # оплаченный месяц — худшее, что тут может случиться.
        logger.warning("Broken pro_until in user_billing: %r", raw)
        return None


@dataclass(frozen=True)
class Allowance:
    """Сколько человек ещё может спросить и почему именно столько.

    `left` — сколько вопросов доступно прямо сейчас (минимум из дневного
    остатка и того, что осталось в месяце с паком). `blocked_by` — что упрётся
    первым, когда left дойдёт до нуля: "day" (пересидеть до завтра) или
    "month" (кончился бесплатный месяц — вот тут и стоит витрина).
    """

    is_pro: bool
    pro_until: Optional[dt.datetime]
    left: int
    day_left: int
    free_left: int
    pack_left: int
    blocked_by: Optional[str]

    @property
    def allowed(self) -> bool:
        return self.left > 0

    @property
    def paywalled(self) -> bool:
        """Упёрся именно в деньги, а не в дневной потолок."""
        return not self.allowed and self.blocked_by == "month"


async def is_pro(user_id: int) -> bool:
    row = await db.get_billing(user_id)
    until = _parse_until(row["pro_until"])
    return bool(until and until > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))


async def allowance(user_id: int) -> Allowance:
    """Полная картина по вопросам: сколько осталось и что кончится первым."""
    row = await db.get_billing(user_id)
    until = _parse_until(row["pro_until"])
    pro = bool(until and until > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

    asked_today = await db.get_ai_question_count_today(user_id)
    daily_cap = config.AI_QUESTION_DAILY_LIMIT_PRO if pro else config.AI_QUESTION_DAILY_LIMIT
    day_left = max(0, daily_cap - asked_today)

    if pro:
        # Месячного счётчика у оплаченного доступа нет вовсе — иначе «без
        # оглядки на счётчик» было бы неправдой.
        return Allowance(
            is_pro=True, pro_until=until, left=day_left, day_left=day_left,
            free_left=0, pack_left=row["pack_questions"],
            blocked_by="day" if day_left == 0 else None,
        )

    asked_month = await db.get_ai_question_count_month(user_id)
    free_left = max(0, config.AI_QUESTION_MONTHLY_FREE - asked_month)
    pack_left = row["pack_questions"]
    budget = free_left + pack_left
    left = min(day_left, budget)
    blocked_by = None
    if left == 0:
        # Дневной потолок называем первым только когда он реально ниже: иначе
        # человеку с кончившимся месяцем предлагали бы «приходи завтра», а
        # завтра ничего не изменится.
        blocked_by = "day" if day_left == 0 and budget > 0 else "month"
    return Allowance(
        is_pro=False, pro_until=until, left=left, day_left=day_left,
        free_left=free_left, pack_left=pack_left, blocked_by=blocked_by,
    )


async def charge_question(user_id: int) -> None:
    """Списать один вопрос — после того, как ответ показан.

    Дневной счётчик двигается всегда (он про нагрузку, а не про деньги), пак —
    только когда бесплатные месяца уже кончились. Порядок именно такой: пак
    покупают, чтобы дожить до следующего месяца, а не чтобы потратить его
    первым.
    """
    row = await db.get_billing(user_id)
    until = _parse_until(row["pro_until"])
    pro = bool(until and until > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    # Считаем ДО инкремента: этот вопрос ещё не в счётчике, и решение «платный
    # он или бесплатный» принимается по состоянию на момент, когда его задали.
    free_used_up = (
        not pro
        and await db.get_ai_question_count_month(user_id) >= config.AI_QUESTION_MONTHLY_FREE
    )
    await db.increment_ai_question_count(user_id)
    if free_used_up:
        await db.consume_pack_question(user_id)


async def video_daily_limit(user_id: int) -> int:
    return config.AI_VIDEO_DAILY_LIMIT_PRO if await is_pro(user_id) else config.AI_VIDEO_DAILY_LIMIT


async def may_offer(user_id: int) -> bool:
    """Пора ли вообще показывать этому человеку платное.

    До PAYWALL_MIN_WORKOUTS закрытых тренировок — нет: он ещё не понял, за что
    платит, и витрина на этом месте читается как «бот оказался платным».
    """
    if not config.stars_enabled():
        return False
    return await db.count_workouts(user_id) >= config.PAYWALL_MIN_WORKOUTS


async def grant(user_id: int, product_code: str) -> str:
    """Выдать купленное. Возвращает строку для человека — что именно он получил."""
    if product_code == PRO_MONTH:
        until = await db.extend_pro(user_id, config.PRO_PERIOD_DAYS)
        return f"доступ к тренеру до {format_until(dt.datetime.fromisoformat(until))}"
    left = await db.add_pack_questions(user_id, config.PACK_QUESTIONS)
    return f"{config.PACK_QUESTIONS} вопросов к тренеру (всего в запасе {left})"


async def revoke(user_id: int, product_code: str) -> None:
    """Забрать выданное при возврате звёзд.

    Ровно то, что выдавали: месяц отматывается назад, пак вычитается. Уже
    потраченное из пака в минус не уводится (см. db.add_pack_questions).
    """
    if product_code == PRO_MONTH:
        await db.extend_pro(user_id, -config.PRO_PERIOD_DAYS)
    else:
        await db.add_pack_questions(user_id, -config.PACK_QUESTIONS)


def format_until(until: dt.datetime) -> str:
    """Дата окончания доступа человеческим языком: «8 сентября»."""
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    return f"{until.day} {months[until.month - 1]}"


def format_revenue(revenue: dict, by_product: list[tuple[str, int, int]]) -> str:
    """Блок про деньги для админского /growth.

    Звёзды переводим в рубли по нижней границе того, что реально доезжает после
    комиссий Telegram (STARS_NET_RUB_PER_STAR).
    """
    head = f"⭐ <b>ЗВЁЗДЫ · {revenue['days']} дн.</b>"
    if not revenue["payments"]:
        if revenue["buyers_total"]:
            return (
                f"{head}\n\nЗа этот срок не платили. Всего платящих за всё время — "
                f"{revenue['buyers_total']}."
            )
        return f"{head}\n\nНи одной оплаты. Витрина — /premium."
    lines = [
        head,
        f"{revenue['stars']} ⭐ за {revenue['payments']} оплат от {revenue['buyers']} чел. "
        f"(~{int(revenue['stars'] * STARS_NET_RUB_PER_STAR)} ₽ на руки).",
        f"Платящих за всё время — {revenue['buyers_total']}.",
    ]
    if by_product:
        lines.append("")
        for code, count, stars in by_product:
            title = PRODUCTS[code].title if code in PRODUCTS else code
            lines.append(f"• {title} — {count} шт., {stars} ⭐")
    return "\n".join(lines)
