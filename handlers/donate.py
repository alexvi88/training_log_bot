"""❤️ «Поддержать проект» — донат звёздами (XTR), без выдачи чего-либо взамен.

НЕ часть монетизации AI-тренера из PR #386: тот пейволл ещё не смержен и
трогает лимиты вопросов, а это — обычный invoice, который ни на что не
влияет и работает независимо (решение — MONETIZATION.md, «Механики», п.4).

Три обязательных шага любого платежа Telegram, и пропуск любого ломает всю
цепочку: счёт (`send_invoice`) → подтверждение (`pre_checkout_query`,
ответить надо за 10 секунд, иначе Telegram сам отменит оплату) → факт
(`successful_payment`). Благодарим и уведомляем админа только на третьем —
до него звёзд ещё нет.

`pre_checkout_query`/`successful_payment` фильтруются по префиксу payload'а
("donate:") нарочно: будущий billing из PR #386 заведёт свои обработчики тех
же типов апдейтов, и без фильтра по payload'у роутеры дрались бы за один и
тот же успешный платёж.
"""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import config
import db
import i18n
import keyboards
import ui

router = Router(name="donate")

logger = logging.getLogger(__name__)


async def _show_donate_screen(target: Message | CallbackQuery) -> None:
    kb = keyboards.donate_keyboard(config.DONATE_PRESETS_STARS)
    text = i18n.t("donate.screen")
    if isinstance(target, CallbackQuery):
        await ui.safe_edit(target, text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "menu:donate")
async def open_donate(callback: CallbackQuery):
    if not config.DONATIONS_ENABLED:
        await callback.answer(i18n.t("donate.disabled_alert"), show_alert=True)
        return
    await _show_donate_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "donate:back")
async def donate_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("donate:pay:"))
async def send_donate_invoice(callback: CallbackQuery):
    """Счёт на звёзды. Отдельным сообщением, а не подменой экрана: счёт в
    Telegram — карточка с кнопкой оплаты, и экран доната под ней должен
    остаться на месте, если человек передумает платить."""
    if not config.DONATIONS_ENABLED:
        await callback.answer(i18n.t("donate.disabled_alert"), show_alert=True)
        return
    stars_str = callback.data.split(":", 2)[2]
    if not stars_str.isdigit() or int(stars_str) not in config.DONATE_PRESETS_STARS:
        await callback.answer(i18n.t("donate.bad_amount"), show_alert=True)
        return
    stars = int(stars_str)
    user_id = callback.from_user.id
    await callback.answer()
    await callback.message.answer_invoice(
        title=i18n.t("donate.invoice.title"),
        description=i18n.t("donate.invoice.description", stars=stars),
        # Payload возвращается в successful_payment нетронутым — по нему и
        # проверяем сумму и владельца счёта. user_id кладём тем же приёмом,
        # что и у будущего billing: платёж, пересланный в другой чат, не
        # должен засчитаться кому-то ещё.
        payload=f"donate:{stars}:{user_id}",
        # Пусто — не забытый параметр, а признак оплаты звёздами.
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{stars} ⭐", amount=stars)],
    )


@router.pre_checkout_query(F.invoice_payload.startswith("donate:"))
async def donate_pre_checkout(query: PreCheckoutQuery):
    """Последняя точка, где можно отказаться, — и ответить надо за 10 секунд.

    Поэтому только проверки по памяти, ни одного похода наружу: payload
    разбирается, сумма сверяется с выставленным счётом, счёт принадлежит
    тому же человеку. Запись и благодарность — уже после денег.

    Язык текста ошибки — той же ценой, без похода в базу: `pre_checkout_query`
    не проходит через `SetUserLanguageMiddleware` (она стоит только на message/
    callback_query, см. main.setup_routers), а лезть в db.get_user здесь значит
    нарушить весь смысл этой функции — держаться в пределах памяти. Берём
    telegram-овскую догадку по `language_code`, тем же способом, что и middleware
    для ещё не заведённого пользователя (см. i18n.normalize).
    """
    lang = i18n.normalize(getattr(query.from_user, "language_code", None))
    parts = query.invoice_payload.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await query.answer(ok=False, error_message=i18n.t_in(lang, "donate.precheckout.parse_error"))
        return
    _, stars_str, owner = parts
    if int(stars_str) != query.total_amount:
        await query.answer(ok=False, error_message=i18n.t_in(lang, "donate.precheckout.amount_mismatch"))
        return
    if owner != str(query.from_user.id):
        await query.answer(ok=False, error_message=i18n.t_in(lang, "donate.precheckout.not_yours"))
        return
    if not config.DONATIONS_ENABLED:
        await query.answer(ok=False, error_message=i18n.t_in(lang, "donate.precheckout.disabled"))
        return
    await query.answer(ok=True)


@router.message(F.successful_payment.invoice_payload.startswith("donate:"))
async def donate_paid(message: Message):
    """Деньги пришли — благодарим и сообщаем админу.

    Идемпотентно: Telegram умеет доставить этот апдейт повторно, и второй
    заход с тем же charge_id не должен слать вторую благодарность или второе
    уведомление (см. db.record_donation) — для человека это один и тот же
    платёж, а не два доната.
    """
    payment = message.successful_payment
    stars = payment.total_amount
    user_id = message.from_user.id

    fresh = await db.record_donation(user_id, payment.telegram_payment_charge_id, stars)
    if not fresh:
        logger.info(
            "Duplicate donate successful_payment %s ignored", payment.telegram_payment_charge_id
        )
        return

    logger.info(
        "Donation: user %s gave %s XTR (charge %s)",
        user_id, stars, payment.telegram_payment_charge_id,
    )
    await message.answer(i18n.t("donate.thanks", stars=stars))
    if config.ADMIN_ID:
        who = f"@{message.from_user.username}" if message.from_user.username else str(user_id)
        try:
            # Уведомление админу — служебное, аудитория один человек (ADMIN_ID),
            # остаётся по-русски независимо от языка донатера (см. i18n_coverage.
            # NEVER_LOCALIZED и тот же приём в handlers/feedback.py).
            await message.bot.send_message(
                config.ADMIN_ID,
                f"❤️ Донат: {who} — {stars} ⭐.\n"
                f"charge_id: <code>{escape(payment.telegram_payment_charge_id)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to notify admin about a donation")
