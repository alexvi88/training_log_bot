"""⭐ Оплата звёздами: витрина, счета, выдача купленного и возврат.

Цифровое внутри бота Telegram разрешает продавать только за Stars, поэтому
провайдера платежей тут нет вовсе: валюта XTR, `provider_token` пустой, а
`prices` — одна строка со стоимостью в звёздах. Правила и остатки — `billing.py`,
экономика — `MONETIZATION.md`.

Три обязательных шага у любого платежа Telegram, и пропуск любого ломает всю
цепочку: счёт (`send_invoice`) → подтверждение (`pre_checkout_query`, ответить
надо за 10 секунд, иначе Telegram сам отменит оплату) → факт
(`successful_payment`). Выдаём только на третьем: до него денег ещё нет.

Возврат — админской командой `/refund <charge_id>`: Telegram сам по звёздам
ничего не возвращает, а мы обещаем вернуть, если не доставили обещанное.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import billing
import config
import db
import keyboards
import ui

router = Router(name="billing")

logger = logging.getLogger(__name__)


_DISABLED_TEXT = (
    "⭐ Платного сейчас нет — тренер работает всем и без него. "
    "Спрашивай в «🤖 AI-тренер»."
)


def _offer_text(allow: billing.Allowance) -> str:
    """Витрина. Сначала — что и так бесплатно: человек должен уходить отсюда
    зная, что дневник у него не отберут, даже если он ничего не купит."""
    pro = billing.PRODUCTS[billing.PRO_MONTH]
    pack = billing.PRODUCTS[billing.QUESTION_PACK]
    head = "⭐ <b>ПЛАТНЫЙ ДОСТУП К ТРЕНЕРУ</b>\n\n"
    if allow.is_pro and allow.pro_until:
        head = (
            "⭐ <b>ТРЕНЕР ОПЛАЧЕН</b>\n\n"
            f"Спрашиваешь без оглядки на счётчик до {billing.format_until(allow.pro_until)}. "
            "Продлить можно прямо сейчас — дни добавлю к оставшимся.\n\n"
        )
    else:
        head += (
            "Дневник, история, графики, программы и выгрузка — бесплатные и такими "
            "останутся. Плачу я только за тренера: каждый его ответ стоит мне денег, "
            f"поэтому бесплатных вопросов {config.AI_QUESTION_MONTHLY_FREE} в месяц.\n\n"
        )
    return (
        head
        + f"<b>{escape(pro.title)}</b> — {config.PRO_PRICE_STARS} ⭐\n"
        + f"{escape(pro.description)}\n\n"
        + f"<b>{escape(pack.title)}</b> — {config.PACK_PRICE_STARS} ⭐\n"
        + f"{escape(pack.description)} Даю {config.PACK_QUESTIONS} штук.\n\n"
        + "Беру звёздами Telegram. Если что-то пойдёт не так — напиши через "
        "«Отзыв / баг / идея», верну звёзды."
    )


async def _show_offer(target: Message | CallbackQuery) -> None:
    user_id = target.from_user.id
    if not config.stars_enabled():
        text, markup = _DISABLED_TEXT, None
    else:
        allow = await billing.allowance(user_id)
        text = _offer_text(allow)
        markup = keyboards.billing_offer(
            pro_stars=config.PRO_PRICE_STARS, pack_stars=config.PACK_PRICE_STARS
        )
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("premium"))
async def cmd_premium(message: Message):
    await _show_offer(message)


@router.callback_query(F.data == "billing:offer")
async def open_offer(callback: CallbackQuery):
    await _show_offer(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("buy:"))
async def send_invoice(callback: CallbackQuery):
    """Счёт на звёзды. Отдельным сообщением, а не подменой экрана: счёт в
    Telegram — это карточка с кнопкой оплаты, и витрина под ней должна
    остаться на месте, если человек передумает платить."""
    if not config.stars_enabled():
        await callback.answer("Платного сейчас нет — тренер и так работает.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    product = billing.PRODUCTS.get(code)
    if product is None:
        await callback.answer("Не понял, что покупаем. Открой витрину заново.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_invoice(
        title=product.title,
        description=product.description,
        # Payload возвращается в successful_payment нетронутым — по нему и
        # решаем, что выдавать. Кладём и код товара, и id покупателя: платёж,
        # пересланный в другой чат, не должен выдавать доступ кому-то ещё.
        payload=f"{code}:{callback.from_user.id}",
        # Пусто — это не забытый параметр, а признак оплаты звёздами.
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=product.title, amount=product.stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Последняя точка, где можно отказаться, — и ответить надо за 10 секунд.

    Поэтому здесь только проверки по памяти и ни одного похода наружу: товар
    известен, продажа включена, платит тот же человек, которому выставляли счёт.
    Всё остальное (выдача, запись) — уже после денег.
    """
    code, _, owner = query.invoice_payload.partition(":")
    if not config.stars_enabled():
        await query.answer(ok=False, error_message="Платное сейчас выключено. Звёзды не списал.")
        return
    if code not in billing.PRODUCTS:
        await query.answer(ok=False, error_message="Такого товара у меня нет. Звёзды не списал.")
        return
    if owner and owner != str(query.from_user.id):
        await query.answer(
            ok=False, error_message="Этот счёт выставлен не тебе. Открой витрину у себя."
        )
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message):
    """Деньги пришли — выдаём.

    Идемпотентно: Telegram умеет доставить этот апдейт повторно, и второй заход
    с тем же charge_id не должен выдавать месяц второй раз (см.
    db.record_star_payment). Человеку при повторе отвечаем то же самое — для
    него это один и тот же платёж, а не ошибка.
    """
    payment = message.successful_payment
    code, _, _ = payment.invoice_payload.partition(":")
    product = billing.PRODUCTS.get(code)
    user_id = message.from_user.id
    if product is None:
        # Товар выкатили и убрали, а счёт у кого-то остался открытым. Деньги
        # уже списаны, поэтому не «неизвестный товар», а разбор через админа.
        logger.error(
            "Paid for unknown product %r, charge %s, user %s",
            code, payment.telegram_payment_charge_id, user_id,
        )
        await message.answer(
            "Звёзды пришли, а товар я не узнал. Напиши через «Отзыв / баг / идея» — "
            "разберусь и верну."
        )
        return

    fresh = await db.record_star_payment(
        user_id,
        payment.telegram_payment_charge_id,
        code,
        payment.total_amount,
        payment.invoice_payload,
    )
    if not fresh:
        logger.info("Duplicate successful_payment %s ignored", payment.telegram_payment_charge_id)
        await message.answer("Этот платёж я уже засчитал. Всё на месте — спрашивай.")
        return

    granted = await billing.grant(user_id, code)
    logger.info(
        "Stars payment: user %s bought %s for %s XTR (charge %s)",
        user_id, code, payment.total_amount, payment.telegram_payment_charge_id,
    )
    await message.answer(
        f"⭐ <b>ЗАПИСАЛ ОПЛАТУ</b>\n\nВключил {escape(granted)}. Спрашивай — "
        "теперь считать вопросы не надо.",
        parse_mode="HTML",
    )
    if config.ADMIN_ID:
        who = f"@{message.from_user.username}" if message.from_user.username else str(user_id)
        try:
            await message.bot.send_message(
                config.ADMIN_ID,
                f"⭐ Оплата: {who} купил {code} за {payment.total_amount} XTR.\n"
                f"charge_id: <code>{escape(payment.telegram_payment_charge_id)}</code>",
                parse_mode="HTML",
            )
        except Exception:  # noqa: BLE001 - уведомление админа не должно ломать выдачу
            logger.exception("Failed to notify admin about payment")


@router.message(Command("refund"))
async def cmd_refund(message: Message):
    """`/refund <charge_id>` — вернуть звёзды и забрать выданное.

    Только админу: команда возвращает чужие деньги и отбирает доступ.
    Telegram по звёздам сам ничего не возвращает, а обещание вернуть, если не
    доставили обещанное, — наше (MONETIZATION.md), и выполняется отсюда.
    """
    if config.ADMIN_ID is None or message.from_user.id != config.ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Скажи, какой платёж возвращаем: /refund &lt;charge_id&gt;. "
            "charge_id я присылаю в уведомлении об оплате.",
            parse_mode="HTML",
        )
        return
    charge_id = parts[1]
    payment = await db.get_star_payment(charge_id)
    if payment is None:
        await message.answer("Такого платежа не нашёл. Проверь charge_id.")
        return
    if payment["refunded_at"]:
        await message.answer("Этот платёж уже возвращён.")
        return
    try:
        await message.bot.refund_star_payment(
            user_id=payment["telegram_id"], telegram_payment_charge_id=charge_id
        )
    except Exception as err:  # noqa: BLE001 - текст ошибки Telegram нужен админу целиком
        logger.exception("Refund failed for charge %s", charge_id)
        await message.answer(f"Telegram отказал в возврате: {escape(str(err))}")
        return
    await db.mark_payment_refunded(charge_id)
    await billing.revoke(payment["telegram_id"], payment["product"])
    await message.answer(
        f"Вернул {payment['stars']} ⭐ пользователю {payment['telegram_id']} "
        f"и забрал {payment['product']}."
    )
