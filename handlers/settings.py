"""User settings: units, e1RM formula, CSV export."""

import csv
import io
import json
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import achievement_sync
import config
import db
import formatting
import i18n
import keyboards
import ui
from fsm import AITrainerFlow, SettingsFlow

router = Router(name="settings")


# Guards "settings:unityes" against a double tap: two callbacks from the same
# user can run concurrently, and the whole rescale (db.get_user →
# scale_user_set_weights → scale_bodyweight_logs → scale_progression_steps →
# db.update_user → achievement_sync.resync) takes seconds — long enough for a
# second tap to still see the old unit and convert the whole history a second
# time. Same shape as handlers.workout._confirming / _try_claim_weight_confirm.
_converting: set[int] = set()


def _try_claim_converting(user_id: int) -> bool:
    """Atomically check-and-reserve `_converting` for this user — no `await`
    between the membership check and the `.add()`, same reasoning as
    ai_trainer._try_claim_busy."""
    if user_id in _converting:
        return False
    _converting.add(user_id)
    return True


async def show_settings(
    callback: CallbackQuery, state: FSMContext, alert: str | None = None, show_alert: bool = True
):
    await state.set_state(SettingsFlow.menu)
    user = await db.get_user(callback.from_user.id)
    kb = keyboards.settings_keyboard(
        user["unit"], user["e1rm_formula"], bool(user["pushes_enabled"]),
        bool(user["ai_comments_enabled"]), bool(user["progression_hint_enabled"]),
        tz_offset=user["tz_offset"],
        food_macros_enabled=bool(user["food_macros_enabled"]),
        show_extra_stats=bool(user["show_extra_stats"]),
        show_mcp=config.mcp_available(),
        lang=user["lang"],
    )
    await ui.safe_edit(callback, i18n.t("settings.screen.title"), reply_markup=kb)
    if alert:
        await callback.answer(alert, show_alert=show_alert)
    else:
        await callback.answer()


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "settings:menu")
async def settings_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат на сам экран настроек — в отличие от settings:back, который
    уводит в главное меню."""
    await show_settings(callback, state)


def profile_rows(user) -> list[tuple[str, Optional[str]]]:
    """Профиль тренирующегося парами «подпись — значение», None у пустых полей.

    Отсюда его берут два места: экран ⚙️ Настройки → 🤖 Что тренер про тебя
    знает и напоминание о памяти на входе в диалог с тренером (см.
    handlers.ai_trainer._memory_reminder). Порядок и подписи общие нарочно:
    человек читает одно и то же в двух местах и не должен гадать, одно ли это.
    """
    equipment = user["equipment"]
    if equipment:
        try:
            items = json.loads(equipment)
            equipment = ", ".join(str(x) for x in items) if isinstance(items, list) else str(equipment)
        except (TypeError, ValueError):
            # Поле пишет модель через json.dumps, но строка могла приехать и из
            # старой записи — показать как есть лучше, чем уронить экран.
            pass
    # Дней в неделю тут нет: тренер их больше не запоминает — это параметр
    # конкретной просьбы («собери на 2 дня»), а не факт о человеке, и
    # запомненная двойка молча определяла все следующие программы (см.
    # ai_trainer._save_athlete_profile). Колонка в базе осталась, но её никто
    # не читает, так что и на экране ей делать нечего.
    return [
        (i18n.t("settings.profile.field.experience"), user["experience"]),
        (i18n.t("settings.profile.field.goal"), user["goal"]),
        (i18n.t("settings.profile.field.equipment"), equipment),
        (i18n.t("settings.profile.field.limitations"), user["limitations"]),
    ]


def _profile_lines(user) -> list[str]:
    """Тот же профиль строками для экрана, пустые поля — прочерком.

    Прочерк, а не пропуск строки: половина ценности экрана в том, чтобы видеть,
    чего тренер про тебя ещё НЕ знает — отсутствующая строка читалась бы как
    «такого поля нет», а не «пусто».
    """
    return [
        f"<b>{label}:</b> {escape(str(value)) if value else '—'}"
        for label, value in profile_rows(user)
    ]


@router.callback_query(F.data == "settings:profile")
async def settings_profile(callback: CallbackQuery, state: FSMContext):
    """«🧬 Обо мне» — что AI-тренер про тебя записал.

    Эти поля он пишет сам, не дожидаясь просьбы (см. save_athlete_profile), и
    до этого экрана их нельзя было ни увидеть, ни поправить — при том что
    именно от них зависит, какую программу он соберёт.
    """
    user = await db.get_user(callback.from_user.id)
    # Состояние, в котором у «просто скажи мне» есть слушатель (см.
    # profile_correction ниже). Без него набранный тут текст попадал в общий
    # «Не понял 🤔 Вопрос тренеру — жми "AI-тренер"», и экран врал прямым
    # текстом: предлагал сказать, как правильно, и не слышал ответа.
    await state.set_state(SettingsFlow.profile)
    lines = _profile_lines(user)
    known = any(not line.endswith("</b> —") for line in lines)
    tail = i18n.t("settings.profile.tail_known" if known else "settings.profile.tail_unknown")
    text = (
        f"🤖 <b>{i18n.t('settings.profile.title')}</b>\n\n"
        f"{i18n.t('settings.profile.intro')}\n\n"
        + "\n".join(lines)
        + f"\n\n{tail}"
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.profile_keyboard(), parse_mode="HTML"
    )
    await callback.answer()


# Инструкция самой модели, не экран: человек её никогда не видит — она
# оборачивает его реплику перед отправкой в ai_trainer._handle_question, и в
# истории чата остаётся только его исходный текст (см. history_question=text
# ниже). Поэтому она остаётся по-русски независимо от языка интерфейса — то же
# решение, что и у SETUP_ANSWERS_FRAME/SETUP_ENOUGH_FRAME в handlers/ai_trainer.py
# (см. i18n_coverage.ALLOWED_CYRILLIC["handlers/settings.py"] для этого модуля).
PROFILE_EDIT_FRAME = (
    "Человек пишет это, глядя на экран «Что я про тебя знаю» — то есть правит "
    "твою память о себе, а не спрашивает про тренировки. Что просит убрать — "
    "стирай через save_athlete_profile с forget, что просит поменять — пиши "
    "туда же новым значением. Потом одной строкой скажи, что стало с профилем.\n\n"
    "Его слова: "
)


@router.message(SettingsFlow.profile, F.text)
async def profile_correction(message: Message, state: FSMContext):
    """«Что-то не так — напиши сюда» — и текст правда уходит тренеру.

    Правку памяти делает он же, тем же инструментом, что и запись: городить
    отдельный редактор на пять полей ради «убери ограничения» — это пять
    экранов там, где хватает одной фразы.
    """
    from handlers import ai_trainer as ai_handler

    text = (message.text or "").strip()
    if not text:
        return
    # Дальше человек уже в разговоре с тренером: он ответит текстом, и логично,
    # что следующая реплика тоже уедет ему, а не упрётся в «Не понял».
    await state.set_state(AITrainerFlow.chatting)
    if not ai_handler._try_claim_busy(message.from_user.id):
        # Та же фраза, что и у остальных «занят прошлым вопросом» в
        # handlers/ai_trainer.py (пока не в этом проходе локализации, см. ответ
        # задачи) — здесь переведена отдельным ключом, без правки того модуля.
        await message.reply(i18n.t("settings.profile.ai_busy"))
        return
    try:
        await ai_handler._handle_question(
            message, state, PROFILE_EDIT_FRAME + text, history_question=text
        )
    finally:
        ai_handler._busy.discard(message.from_user.id)


@router.callback_query(F.data == "settings:profileclear")
async def settings_profile_clear(callback: CallbackQuery, state: FSMContext):
    await db.update_user(
        callback.from_user.id,
        days_per_week=None, experience=None, goal=None, equipment=None, limitations=None,
    )
    await callback.answer(i18n.t("settings.profile.clear_alert"))
    await settings_profile(callback, state)


@router.callback_query(F.data == "settings:unit")
async def settings_unit_confirm(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_unit = "lb" if user["unit"] == "kg" else "kg"
    unit_name = keyboards.UNIT_NAMES[new_unit]
    kb = keyboards.yes_no_keyboard(
        yes_cb="settings:unityes", no_cb="settings:unitno",
        yes_text=i18n.t("settings.unit.switch_to", unit=unit_name), no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(
        callback,
        i18n.t("settings.unit.confirm", unit=unit_name),
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
    """Claims `_converting` before the first `await`: two fast taps on
    "Переключить на lb" both read `db.get_user` while the unit is still "kg"
    (each holds its own snapshot), so without this guard both would rescale
    the whole history — the second tap doubling every weight instead of
    finding nothing left to convert."""
    user_id = callback.from_user.id
    if not _try_claim_converting(user_id):
        await callback.answer(i18n.t("settings.unit.busy"))
        return
    try:
        user = await db.get_user(user_id)
        old_unit = user["unit"]
        new_unit = "lb" if old_unit == "kg" else "kg"
        factor = config.LB_PER_KG if new_unit == "lb" else 1 / config.LB_PER_KG
        await db.scale_user_set_weights(user_id, factor)
        await db.scale_bodyweight_logs(user_id, factor)
        # Шаг прогрессии в программах хранится в единицах пользователя, как и веса
        # подходов, — без пересчёта «+2.5 кг» после переключения читалось бы как «+2.5 lb».
        await db.scale_progression_steps(user_id, factor)
        await db.update_user(user_id, unit=new_unit)
        await _rescale_active_workout_weight_cache(state, factor)
        # Badge thresholds are in kilograms and the stored weights just changed unit,
        # so what the user qualifies for has to be recomputed both ways — resync
        # revokes as well as awards, unlike the award-only path used at finish time.
        await achievement_sync.resync(user_id)
        await show_settings(
            callback, state,
            alert=i18n.t("settings.unit.done", unit=keyboards.UNIT_NAMES[new_unit]),
        )
    finally:
        _converting.discard(user_id)


@router.callback_query(F.data == "settings:unitno")
async def settings_unit_cancel(callback: CallbackQuery, state: FSMContext):
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:tz")
async def settings_timezone(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await ui.safe_edit(
        callback,
        i18n.t("settings.tz.prompt"),
        reply_markup=keyboards.timezone_picker_keyboard(user["tz_offset"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:tzset:"))
async def settings_timezone_set(callback: CallbackQuery, state: FSMContext):
    offset = int(callback.data.split(":")[2])
    user = await db.get_user(callback.from_user.id)
    changed = user is not None and user["tz_offset"] != offset
    await db.update_user(callback.from_user.id, tz_offset=offset)
    # Отдельная отметка от самого значения: дефолт у новичка теперь и так не
    # ноль (см. config.DEFAULT_TZ_OFFSET), так что «пояс осознанно выбран» —
    # это факт про действие, а не про число. См. engagement._deliver — на неё
    # опирается подсказка «пуш пришёл не вовремя?» под первым пушем.
    await db.mark_tz_set_by_user(callback.from_user.id)
    if changed:
        # Часть значков считается по календарным дням, а дни — местные
        # (db.list_finished_workout_dates): стрик по неделям, пара выходных,
        # все дни недели, 31 декабря. Сдвинул пояс — тренировка в 23:00 уехала
        # на соседние сутки, и набор значков стал другим. Без пересчёта здесь
        # он оставался прежним навсегда: путь при завершении тренировки умеет
        # только выдавать, но не отбирать.
        await achievement_sync.resync(callback.from_user.id)
    await show_settings(
        callback, state,
        alert=i18n.t("settings.tz.set_alert", offset=keyboards.format_utc_offset(offset)),
        show_alert=False,
    )


@router.callback_query(F.data == "settings:tzback")
async def settings_timezone_back(callback: CallbackQuery, state: FSMContext):
    await show_settings(callback, state)


@router.callback_query(F.data == "settings:lang")
async def settings_language(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await ui.safe_edit(
        callback,
        i18n.t_in(user["lang"], "screen.language.title"),
        reply_markup=keyboards.language_keyboard(user["lang"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:langset:"))
async def settings_language_set(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split(":")[2]
    if lang not in i18n.SUPPORTED:
        # Незнакомый код в callback (старая клавиатура, чужой клиент) — не
        # роняем хендлер и не трогаем базу, просто перерисовываем экран как есть.
        await settings_language(callback, state)
        return
    from handlers.persistent_menu import attach_silently

    await db.set_user_lang(callback.from_user.id, lang)
    # Порядок важен: контекст переставляем ДО перерисовки экрана настроек —
    # иначе человек нажмёт English и увидит русский экран (см. задачу).
    i18n.set_lang(lang)
    # Нижнюю клавиатуру НАДО перевыслать: она прикрепляется один раз и живёт в
    # чате с теми подписями, что были при отправке. Экраны перерисовываются, а
    # она — нет, и человек оставался с «Workout / Menu / AI Coach» под русским
    # ботом. Нажатия при этом работали (BTN_* сравнивают себя со всеми языками
    # сразу), поэтому баг был чисто визуальным и оттого незаметным в тестах.
    #
    # Молча, а не через уведомление мидлвари: человек только что сам сменил
    # язык и не нуждается в объяснении, почему кнопки стали другими.
    await attach_silently(callback.message, callback.from_user.id)
    await show_settings(
        callback, state,
        alert=i18n.t_in(lang, "screen.language.set_alert"),
        show_alert=False,
    )


@router.callback_query(F.data == "settings:formula")
async def settings_formula_confirm(callback: CallbackQuery, state: FSMContext):
    """Switching the formula recomputes every e1RM, record and chart in the app's
    history. That's a bigger change than switching units (which at least keeps the
    numbers meaning the same thing), so it asks the same way."""
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    formula_name = keyboards.FORMULA_NAMES[new_formula]
    kb = keyboards.yes_no_keyboard(
        yes_cb="settings:formulayes", no_cb="settings:formulano",
        yes_text=i18n.t("settings.formula.switch_to", formula=formula_name), no_text=i18n.t("btn.cancel"),
    )
    await ui.safe_edit(
        callback,
        i18n.t("settings.formula.confirm", formula=formula_name),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "settings:formulayes")
async def settings_formula(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    new_formula = "brzycki" if user["e1rm_formula"] == "epley" else "epley"
    await db.update_user(callback.from_user.id, e1rm_formula=new_formula)
    await show_settings(
        callback, state,
        alert=i18n.t("settings.formula.done", formula=keyboards.FORMULA_NAMES[new_formula]),
        show_alert=False,
    )


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
        BufferedInputFile(data, filename="training_log.csv"), caption=i18n.t("settings.export.caption")
    )
    await callback.answer()
