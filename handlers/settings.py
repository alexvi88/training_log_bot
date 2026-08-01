"""User settings: units, e1RM formula, CSV export."""

import csv
import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

import config
import db
import formatting
import keyboards
import stickers
import ui
from fsm import SettingsFlow

router = Router(name="settings")


async def show_settings(callback: CallbackQuery, state: FSMContext, alert: str | None = None):
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(callback.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["e1rm_formula"], bool(user["pushes_enabled"]),
        bool(user["ai_comments_enabled"]), bool(user["progression_hint_enabled"]),
        tz_offset=user["tz_offset"],
        stickers_enabled=bool(user["stickers_enabled"]),
        show_stickers_toggle=stickers.is_configured(),
    )
    await ui.safe_edit(callback, "🔧 Настройки:", reply_markup=kb)
    if alert:
        await callback.answer(alert, show_alert=True)
    else:
        await callback.answer()


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "settings:unit")
async def settings_unit_confirm(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_unit = "lb" if user["unit"] == "kg" else "kg"
    kb = keyboards.yes_no_keyboard(
        yes_cb="settings:unityes", no_cb="settings:unitno",
        yes_text=f"Переключить на {new_unit}", no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"Переключить единицы на {new_unit}? Все веса в истории будут пересчитаны — "
        "конвертация туда-обратно теряет точность на округлении.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "settings:unityes")
async def settings_unit(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await db.get_user(user_id)
    old_unit = user["unit"]
    new_unit = "lb" if old_unit == "kg" else "kg"
    factor = config.LB_PER_KG if new_unit == "lb" else 1 / config.LB_PER_KG
    await db.scale_user_set_weights(user_id, factor)
    await db.scale_bodyweight_logs(user_id, factor)
    await db.update_user(user_id, unit=new_unit)
    await show_settings(
        callback, state,
        alert=f"Единицы переключены на {new_unit}. Все веса в истории пересчитаны автоматически.",
    )


@router.callback_query(F.data == "settings:unitno")
async def settings_unit_cancel(callback: CallbackQuery, state: FSMContext):
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:tz")
async def settings_timezone(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await ui.safe_edit(
        callback,
        "🕒 Выбери свой часовой пояс (сдвиг от UTC).\n"
        "Это влияет на «сегодня»/«вчера» и на время, к которому бот считает твой день.",
        reply_markup=keyboards.timezone_picker_keyboard(user["tz_offset"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:tzset:"))
async def settings_timezone_set(callback: CallbackQuery, state: FSMContext):
    offset = int(callback.data.split(":")[2])
    await db.update_user(callback.from_user.id, tz_offset=offset)
    await show_settings(callback, state, alert=f"Часовой пояс: {keyboards.format_utc_offset(offset)}")


@router.callback_query(F.data == "settings:tzback")
async def settings_timezone_back(callback: CallbackQuery, state: FSMContext):
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:formula")
async def settings_formula_confirm(callback: CallbackQuery, state: FSMContext):
    """Switching the formula recomputes every e1RM, record and chart in the app's
    history. That's a bigger change than switching units (which at least keeps the
    numbers meaning the same thing), so it asks the same way."""
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    kb = keyboards.yes_no_keyboard(
        yes_cb="settings:formulayes", no_cb="settings:formulano",
        yes_text=f"Переключить на {new_formula}", no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback,
        f"e1RM — расчётный разовый максимум: сколько бы ты поднял на один раз. "
        f"Считается из веса и повторов, и {new_formula} — просто другая формула "
        f"этого расчёта.\n\n"
        f"Переключить на {new_formula}? Все расчётные максимумы, "
        "рекорды и графики пересчитаются — сами подходы не изменятся.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "settings:formulayes")
async def settings_formula(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    await db.update_user(callback.from_user.id, e1rm_formula=new_formula)
    await show_settings(callback, state, alert=f"Формула e1RM: {new_formula}")


@router.callback_query(F.data == "settings:formulano")
async def settings_formula_cancel(callback: CallbackQuery, state: FSMContext):
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:progression")
async def settings_progression(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(
        callback.from_user.id, progression_hint_enabled=0 if user["progression_hint_enabled"] else 1
    )
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:pushes")
async def settings_pushes(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, pushes_enabled=0 if user["pushes_enabled"] else 1)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:ai_comments")
async def settings_ai_comments(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, ai_comments_enabled=0 if user["ai_comments_enabled"] else 1)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:stickers")
async def settings_stickers(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(callback.from_user.id, stickers_enabled=0 if user["stickers_enabled"] else 1)
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:export")
async def settings_export(callback: CallbackQuery, state: FSMContext):
    rows = await db.export_rows_for_user(callback.from_user.id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["started_at", "exercise", "round_index", "weight", "reps", "rpe"])
    for r in rows:
        writer.writerow([
            r["started_at"], r["exercise"], r["round_index"], r["weight"], r["reps"],
            "" if r["rpe"] is None else formatting.format_weight(r["rpe"]),
        ])
    data = buf.getvalue().encode("utf-8-sig")
    await callback.message.answer_document(
        BufferedInputFile(data, filename="training_log.csv"), caption="Экспорт истории тренировок"
    )
    await callback.answer()
