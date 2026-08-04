"""User settings: units, e1RM formula, CSV export."""

import csv
import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

import achievement_sync
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
        food_macros_enabled=bool(user["food_macros_enabled"]),
        show_extra_stats=bool(user["show_extra_stats"]),
        show_mcp=config.mcp_available(),
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


async def _rescale_active_workout_weight_cache(state: FSMContext, factor: float) -> None:
    """Rescale the in-progress workout's FSM weight caches by the same factor
    used on the DB, so a unit switch mid-workout doesn't leave them stuck in
    the old unit.

    `db.scale_user_set_weights` only touches rows already on disk — but
    `last_by_exercise` (carry-forward for bare "8" input), `last_session_sets`
    (the "в прошлый раз" hint), `weight_steps` (progression step) and
    `confirmed_weights` (the "555кг? да/нет" answer) are cached in FSM state
    for the exercises already open this session, and without this they'd keep
    answering in the unit the user just switched away from — e.g. "8" would
    carry forward 100 as if it were still kg right after switching to lb.
    """
    data = await state.get_data()
    updates: dict = {}

    last_by = data.get("last_by_exercise")
    if last_by:
        updates["last_by_exercise"] = {
            ex_id: (weight * factor, reps) for ex_id, (weight, reps) in last_by.items()
        }

    last_session_sets = data.get("last_session_sets")
    if last_session_sets:
        updates["last_session_sets"] = {
            ex_id: [(weight * factor, reps, rpe) for weight, reps, rpe in sets]
            for ex_id, sets in last_session_sets.items()
        }

    for key in ("weight_steps", "confirmed_weights"):
        values = data.get(key)
        if values:
            updates[key] = {ex_id: value * factor for ex_id, value in values.items()}

    if updates:
        await state.update_data(**updates)


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
    await _rescale_active_workout_weight_cache(state, factor)
    # Badge thresholds are in kilograms and the stored weights just changed unit,
    # so what the user qualifies for has to be recomputed both ways — resync
    # revokes as well as awards, unlike the award-only path used at finish time.
    await achievement_sync.resync(user_id)
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
        f"e1RM — расчётный максимум в упражнении: какой вес ты смог бы поднять на один раз. "
        f"Считается по весу и повторам, а {new_formula} — просто другая формула "
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


@router.callback_query(F.data == "settings:card_detail")
async def settings_card_detail(callback: CallbackQuery, state: FSMContext):
    """Detailed cards carry the e1RM line under each exercise; compact ones drop
    it. The column existed from the start but had no switch."""
    user = await db.get_user(callback.from_user.id)
    await db.update_user(
        callback.from_user.id, show_extra_stats=0 if user["show_extra_stats"] else 1
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


@router.callback_query(F.data == "settings:food_macros")
async def settings_food_macros(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await db.update_user(
        callback.from_user.id, food_macros_enabled=0 if user["food_macros_enabled"] else 1
    )
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
