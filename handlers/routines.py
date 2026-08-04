"""🗂 Программы — saved workout templates (splits).

A routine is an ordered list of exercises. Starting a workout from a routine
fills the FSM's `planned_blocks` so the existing "▶️ Следующее по программе"
flow (handlers/workout.py) walks the user through it one exercise at a time.
Routines can be created from any past finished workout — do the session
once, then save it as your split.
"""

import datetime as dt
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import formatting
import keyboards
import timeutil
import ui
import view_builder
from fsm import RoutineFlow, WorkoutFlow
from seed_data import PROGRAM_BY_KEY, WORKOUT_PROGRAMS

router = Router(name="routines")

ROUTINE_SOURCE_PAGE_SIZE = 8


async def show_manage(event, state: FSMContext) -> None:
    user_id = event.from_user.id
    # Многодневки свёрнуты в одну строку каждая, дни — за вторым экраном
    # (rt_program_days): иначе один добавленный сплит занимает три-четыре кнопки.
    programs = await db.list_programs(user_id)
    routines = await db.list_standalone_routines(user_id)
    has_workouts = await db.count_workouts(user_id) > 0
    if programs or routines:
        text = "🗂 <b>ПРОГРАММЫ</b>\n\nВыбери программу или создай новую."
    else:
        # Порядок предложений не случаен: готовая программа появляется в один
        # тап и ничего не стоит, а сборка с тренером — это переписка. Раньше все
        # три способа были равнозначны, и выбирать между ними приходилось до
        # того, как человек понял, что вообще выбирает.
        text = (
            "🗂 <b>ПРОГРАММЫ</b>\n\nУ тебя пока нет сохранённых программ.\n\n"
            "Быстрее всего — взять <b>готовую</b>: её дни появятся здесь сразу, и "
            "тренировка начнётся в один тап.\n"
            "Хочешь под себя — <b>AI-тренер</b> спросит пару вещей и соберёт.\n"
            "А если уже потренировался — можно сохранить эту тренировку как программу."
        )
    kb = keyboards.routines_manage_keyboard(programs, routines, has_workouts=has_workouts)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "rt:manage")
async def rt_manage(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_manage(callback, state)
    await callback.answer()


async def _owned_program(event, program_id: int):
    program = await db.get_program(program_id)
    if program is None or program["user_id"] != event.from_user.id:
        if isinstance(event, CallbackQuery):
            await event.answer("Программа не найдена", show_alert=True)
        return None
    return program


def _days_ago_label(iso: str, today: dt.date) -> str:
    days = (today - dt.datetime.fromisoformat(iso).date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} {formatting.plural_ru(days, ('день', 'дня', 'дней'))} назад"


async def _show_program(event, state: FSMContext, program_id: int) -> None:
    """Экран одной программы: что в ней, какой день сегодня и когда что делалось.

    Раньше это был просто список одинаковых кнопок с именами дней: какой из них
    следующий, человек держал в голове сам — при том, что бот это знает
    (workouts.routine_id пишется с самого начала и до сих пор кормил ровно один
    экран). Давность по каждому дню тут же показывает и перекос: «ноги — 19 дней
    назад» видно, не открывая ничего.
    """
    program = await _owned_program(event, program_id)
    if program is None:
        return
    days = await db.list_program_days_by_id(program_id)
    history = await db.program_day_history(program_id)
    today = timeutil.user_today(await db.get_user(event.from_user.id))
    next_day = await db.next_program_day(program_id)

    day_blocks = []
    for day in days:
        exercises = await db.list_routine_exercises(day["id"])
        ex_lines = [
            f"• {escape(ex['display_name'])}" + (f" — {escape(ex['target'])}" if ex["target"] else "")
            for ex in exercises
        ] or ["В дне нет упражнений (возможно, они были архивированы)."]
        entry = history.get(day["id"])
        when = f" <i>· {_days_ago_label(entry[0], today)}</i>" if entry else " <i>· ещё не делал</i>"
        day_blocks.append("\n".join([f"<b>{escape(day['name'])}</b>{when}", *ex_lines]))

    header = [f"🗂 <b>{escape(program['name'])}</b>"]
    total = sum(entry[1] for entry in history.values())
    if total:
        word = formatting.plural_ru(total, ("тренировка", "тренировки", "тренировок"))
        header.append(f"<i>{total} {word} по ней</i>")
    tail = (
        f"Дальше по кругу — <b>{escape(next_day['name'])}</b>."
        if next_day is not None and history
        else "Выбери день — посмотреть состав или начать тренировку."
    )
    text = "\n\n".join(["\n".join(header), "\n\n".join(day_blocks) or "В программе нет дней.", tail])
    kb = keyboards.program_days_keyboard(
        days, program_id, next_day_id=next_day["id"] if next_day else None
    )
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("rt:prg:"))
async def rt_program(callback: CallbackQuery, state: FSMContext):
    await _show_program(callback, state, int(callback.data.split(":")[2]))
    await callback.answer()


@router.callback_query(F.data.startswith("rt:pgmedit:"))
async def rt_program_edit(callback: CallbackQuery, state: FSMContext):
    """«⚙️ Изменить программу» — отдельный экран под все правки.

    Состав дней тут не повторяется: его человек только что видел на экране
    программы, а здесь он выбирает действие, а не читает программу. Число дней
    оставлено — от него зависит, что вообще осмысленно нажимать («Порядок дней»
    на одном дне не показывается).
    """
    program_id = int(callback.data.split(":")[2])
    program = await _owned_program(callback, program_id)
    if program is None:
        return
    days = await db.list_program_days_by_id(program_id)
    word = formatting.plural_ru(len(days), ("день", "дня", "дней"))
    text = (
        f"⚙️ <b>{escape(program['name'])}</b>\n"
        f"<i>{len(days)} {word}</i>\n\n"
        "Что меняем?"
    )
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.program_edit_keyboard(days, program_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:pgm:"))
async def rt_program_days_legacy(callback: CallbackQuery, state: FSMContext):
    """Старая ручка экрана программы — id одного из её дней.

    Программа не имела собственного id, и экран открывался «якорем»
    MAX(routine.id). Такие кнопки живут в чатах пользователей и после релиза,
    поэтому разрешаем их в программу, а не роняем: сменить префикс дешевле, чем
    переучить уже отправленные сообщения (и безопаснее, чем переиспользовать
    rt:pgm под новые id — тогда старая кнопка открыла бы чужую программу).
    """
    anchor = await _owned_routine(callback, int(callback.data.split(":")[2]))
    if anchor is None:
        return
    if anchor["program_id"] is None:
        await _show_routine_detail(callback, state, anchor["id"])
    else:
        await _show_program(callback, state, anchor["program_id"])
    await callback.answer()


@router.callback_query(F.data == "rt:menu")
async def rt_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu
    await _show_main_menu(callback, state)
    await callback.answer()


# ---------- ready-made programs ----------

@router.callback_query(F.data == "rt:programs")
async def rt_programs(callback: CallbackQuery, state: FSMContext):
    text = (
        "✨ <b>ГОТОВЫЕ ПРОГРАММЫ</b>\n\n"
        "Выбери готовую программу — её дни добавятся тебе в «Программы», и ты "
        "начнёшь тренировку в один тап. Все нужные упражнения появятся в твоём списке."
    )
    kb = keyboards.programs_catalog_keyboard(WORKOUT_PROGRAMS)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rt:prog:"))
async def rt_program_detail(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    days = program["days"]
    # Разбор по дням — плоским текстом, без сворачивающегося блока: состав как
    # раз то, по чему решают, брать программу или нет, а тоггл прячет его за
    # лишним тапом (то же самое сделано на экране дней своей программы).
    day_blocks = [
        "\n".join([
            f"<b>{escape(day_name)}</b>",
            *(f"• {escape(ex)} — {escape(target)}" for ex, target in exercises),
        ])
        for day_name, exercises in days
    ]
    text = "\n\n".join([
        f"✨ <b>{escape(program['name'])}</b>\n<i>{escape(program['meta'])}</i>",
        escape(program["description"]),
        f"<b>{len(days)} {_days_word(len(days))}:</b>\n" + "\n\n".join(day_blocks),
    ])
    kb = keyboards.program_detail_keyboard(key)
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _instantiate_catalog_program(user_id: int, key: str, name: str) -> int:
    program_id = await db.create_program(
        user_id, name, source="catalog", source_ref=key
    )
    for day_name, exercises in PROGRAM_BY_KEY[key]["days"]:
        await db.create_routine_from_program(user_id, day_name, exercises, program_id=program_id)
    return program_id


@router.callback_query(F.data.startswith("rt:progadd:"))
async def rt_program_add(callback: CallbackQuery, state: FSMContext):
    """«➕ Добавить себе» на каталожной программе.

    Раньше это был безусловный INSERT: второй тап (а на подвисшей связи он
    случается сам собой) дописывал те же дни в программу с тем же именем, и
    трёхдневный PPL превращался в «PPL · 9 дней» из трёх одинаковых троек,
    неразличимых по названию. Теперь имя занято → спрашиваем.
    """
    key = callback.data.split(":", 2)[2]
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    user_id = callback.from_user.id
    over_budget = await db.routine_budget(user_id, len(program["days"]))
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return

    existing = await db.find_program_by_name(user_id, program["name"])
    if existing is not None:
        await ui.safe_edit(
            callback,
            f"У тебя уже есть программа «{escape(program['name'])}».\n"
            "Открыть её или добавить вторую копию?",
            reply_markup=keyboards.program_name_taken_keyboard(
                existing["id"], back_cb=f"rt:prog:{key}", add_cb=f"rt:progadd2:{key}"
            ),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    program_id = await _instantiate_catalog_program(user_id, key, program["name"])
    await callback.answer(f"Программа добавлена: {len(program['days'])} дн.")
    await _show_program(callback, state, program_id)


@router.callback_query(F.data.startswith("rt:progadd2:"))
async def rt_program_add_copy(callback: CallbackQuery, state: FSMContext):
    """«Добавить второй копией» — тот же каталожный сплит под свободным именем.
    Иногда это осмысленно (вторая версия под правку), просто не молча."""
    key = callback.data.split(":", 2)[2]
    program = PROGRAM_BY_KEY.get(key)
    if program is None:
        await callback.answer("Программа не найдена", show_alert=True)
        return
    user_id = callback.from_user.id
    over_budget = await db.routine_budget(user_id, len(program["days"]))
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return
    name = await db.unique_program_name(user_id, program["name"])
    program_id = await _instantiate_catalog_program(user_id, key, name)
    await callback.answer(f"Добавил как «{name}»")
    await _show_program(callback, state, program_id)


def _days_word(n: int) -> str:
    """Russian plural for «день» (1 день, 2 дня, 5 дней)."""
    if 11 <= n % 100 <= 14:
        return "дней"
    last = n % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


async def _owned_routine(event, routine_id: int):
    routine = await db.get_routine(routine_id)
    if routine is None or routine["user_id"] != event.from_user.id:
        if isinstance(event, CallbackQuery):
            await event.answer("Программа не найдена", show_alert=True)
        return None
    return routine


async def _show_routine_detail(event, state: FSMContext, routine_id: int) -> None:
    routine = await _owned_routine(event, routine_id)
    if routine is None:
        return
    exercises = await db.list_routine_exercises(routine_id)
    program_name = routine["program_name"]
    if program_name:
        lines = [
            f"🗂 <b>{escape(program_name)}</b>",
            f"<b>{escape(routine['name'])}</b>",
            "",
        ]
    else:
        lines = [f"🗂 <b>{escape(routine['name'])}</b>", ""]
    if exercises:
        for i, ex in enumerate(exercises, start=1):
            suffix = f" — {escape(ex['target'])}" if ex["target"] else ""
            lines.append(f"{i}. {escape(ex['display_name'])}{suffix}")
    else:
        lines.append("Здесь нет упражнений (возможно, они были архивированы).")
    kb = keyboards.routine_detail_keyboard(routine_id, program_id=routine["program_id"])
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


async def _show_routine_editor(event, state: FSMContext, routine_id: int) -> None:
    routine = await _owned_routine(event, routine_id)
    if routine is None:
        return
    exercises = await db.list_routine_exercises(routine_id)
    title = f"{routine['program_name']} · {routine['name']}" if routine["program_name"] else routine["name"]
    lines = [f"✏️ <b>{escape(title)}</b>", ""]
    if exercises:
        lines.append("✏️ — поменять схему подходов, 🗑 — убрать упражнение.")
    else:
        lines.append("Здесь пока нет упражнений.")
    kb = keyboards.routine_edit_keyboard(
        routine_id,
        [
            (
                ex["id"],
                f"{ex['display_name']} — {ex['target']}" if ex["target"] else ex["display_name"],
                ex["target"],
            )
            for ex in exercises
        ],
    )
    text = "\n".join(lines)
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("rt:view:"))
async def rt_view(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    await _show_routine_detail(callback, state, routine_id)
    await callback.answer()


@router.callback_query(F.data.startswith("rt:editmenu:"))
async def rt_edit_menu(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    await ui.safe_edit(
        callback, f"✏️ <b>{escape(routine['name'])}</b>",
        reply_markup=keyboards.routine_edit_menu_keyboard(
            routine_id, is_day=routine["program_id"] is not None
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:edit:"))
async def rt_edit(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    await _show_routine_editor(callback, state, routine_id)
    await callback.answer()


# Button text has a hard Telegram limit (64 chars) and gets cramped well before
# that, so the exercise summary is cut short rather than listing everything.
_SOURCE_PICKER_SUMMARY_MAX = 30


async def _workout_exercise_names(workout_id: int) -> list[str]:
    """Exercise names for a workout, in block order and de-duplicated — same
    source list create_routine_from_workout snapshots into the routine."""
    seen: set[int] = set()
    names: list[str] = []
    for block in await db.list_blocks_for_workout(workout_id):
        for be in await db.get_block_exercises(block["id"]):
            if be["exercise_id"] in seen:
                continue
            seen.add(be["exercise_id"])
            names.append(be["display_name"])
    return names


async def _workout_exercise_summary(workout_id: int) -> str:
    """Comma-joined, truncated version of _workout_exercise_names for a button label."""
    summary = ", ".join(await _workout_exercise_names(workout_id))
    if len(summary) > _SOURCE_PICKER_SUMMARY_MAX:
        summary = summary[:_SOURCE_PICKER_SUMMARY_MAX].rstrip() + "…"
    return summary


async def _show_routine_source_picker(callback: CallbackQuery, state: FSMContext, page: int) -> None:
    user_id = callback.from_user.id
    total = await db.count_workouts(user_id)
    workouts = await db.list_workouts(user_id, limit=ROUTINE_SOURCE_PAGE_SIZE, offset=page * ROUTINE_SOURCE_PAGE_SIZE)
    items = []
    for w in workouts:
        date_label = formatting.format_date_ru(dt.datetime.fromisoformat(w["started_at"]))
        summary = await _workout_exercise_summary(w["id"])
        label = f"{date_label} — {summary}" if summary else date_label
        items.append({"id": w["id"], "label": label})
    has_next = (page + 1) * ROUTINE_SOURCE_PAGE_SIZE < total
    kb = keyboards.routine_source_picker_keyboard(items, page, has_next)
    text = "Из какой тренировки создать программу?" if items else "Нет завершённых тренировок."
    await state.update_data(routine_source_page=page)
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(F.data.startswith("rt:pickw:page:"))
async def rt_pickw_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[3])
    await _show_routine_source_picker(callback, state, page)
    await callback.answer()


async def _show_routine_source_preview(callback: CallbackQuery, workout_id: int) -> None:
    """Full exercise list of the tapped workout, with a confirm/back choice — the
    picker's button label is truncated, so this is where the user actually sees
    what they're about to base a program on."""
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    date_label = formatting.format_date_ru(dt.datetime.fromisoformat(workout["started_at"]))
    blocks = await view_builder.build_block_views(workout_id)
    lines = [f"📋 <b>{escape(date_label)}</b>", ""]
    if blocks:
        for i, b in enumerate(blocks, start=1):
            sets_str = ", ".join(
                formatting.format_set(w, r, rpe)
                for (w, r), rpe in zip(b.sets, b.set_rpes or [None] * len(b.sets), strict=True)
            )
            lines.append(f"{i}. <b>{escape(b.exercise_name)}</b>")
            if sets_str:
                lines.append(f"   {sets_str}")
    else:
        lines.append("В тренировке нет упражнений.")
    lines.append("")
    lines.append("Создать программу из этой тренировки?")
    kb = keyboards.routine_source_preview_keyboard(workout_id)
    await ui.safe_edit(callback, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("rt:pickw:item:"))
async def rt_pickw_item(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[3])
    await _show_routine_source_preview(callback, workout_id)
    await callback.answer()


@router.callback_query(F.data == "rt:pickw:back")
async def rt_pickw_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _show_routine_source_picker(callback, state, data.get("routine_source_page", 0))
    await callback.answer()


@router.callback_query(F.data.startswith("rt:pickw:use:"))
async def rt_pickw_use(callback: CallbackQuery, state: FSMContext):
    workout_id = int(callback.data.split(":")[3])
    workout = await db.get_workout(workout_id)
    if workout is None or workout["user_id"] != callback.from_user.id:
        await callback.answer("Тренировка не найдена", show_alert=True)
        return
    over_budget = await db.routine_budget(callback.from_user.id, 1)
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return
    data = await state.get_data()
    program_id = data.get("day_program_id") if data.get("day_from_workout") else None
    await state.set_state(RoutineFlow.naming)
    await state.update_data(routine_source_workout_id=workout_id)
    what = "день" if program_id else "программу"
    await ui.safe_edit(
        callback,
        f"Как назвать {what}? (например «День груди» или «Тяни»)",
        reply_markup=keyboards.cancel_keyboard(f"rt:prg:{program_id}" if program_id else "rt:manage"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:daypickw:"))
async def rt_day_from_workout(callback: CallbackQuery, state: FSMContext):
    """«➕ Добавить день → 🏋️ Из прошлой тренировки»: тот же выбор исходной
    тренировки, что и для новой программы, только результат становится днём уже
    существующей."""
    _, _, program_id_s, page_s = callback.data.split(":")
    program_id = int(program_id_s)
    if await _owned_program(callback, program_id) is None:
        return
    await state.update_data(day_program_id=program_id, day_from_workout=True, day_copy_from=None)
    await _show_routine_source_picker(callback, state, int(page_s))
    await callback.answer()


@router.message(StateFilter(RoutineFlow.naming), F.text)
async def rt_name_entered(message: Message, state: FSMContext):
    """Снимок тренировки как программа — или как ещё один день существующей.

    Схему подходов теперь берём из того, что человек реально сделал
    (db.create_routine_from_workout): этот путь был единственным из четырёх, где
    программа приезжала вообще без схемы, хотя подходы лежали рядом.
    """
    name, error = _valid_name(message.text)
    if error:
        await message.reply(error)
        return
    data = await state.get_data()
    workout_id = data["routine_source_workout_id"]
    program_id = data.get("day_program_id") if data.get("day_from_workout") else None
    if program_id is not None and await _owned_program(message, program_id) is None:
        program_id = None
    routine_id = await db.create_routine_from_workout(
        message.from_user.id, workout_id, name, program_id=program_id
    )
    await state.set_state(None)
    await state.update_data(day_from_workout=None, day_program_id=None)
    await _show_routine_detail(message, state, routine_id)
    if program_id is None:
        # Один снимок — это одиночная программа; человек, который полгода ходит
        # по своему А/Б, раньше получал две несвязанные строки в списке и никакого
        # способа их поженить. Предлагаем сделать из этого многодневку сразу.
        await message.answer(
            "Ходишь по нескольким разным тренировкам? Можно собрать из них одну "
            "программу с днями.",
            reply_markup=keyboards.yes_no_keyboard(
                yes_cb=f"rt:tomulti:{routine_id}", no_cb="rt:manage",
                yes_text="🗂 Собрать программу", no_text="Не надо",
            ),
        )


@router.callback_query(F.data.startswith("rt:tomulti:"))
async def rt_to_multiday(callback: CallbackQuery, state: FSMContext):
    """Превратить только что снятую одиночную программу в первый день многодневки."""
    routine = await _owned_routine(callback, int(callback.data.split(":")[2]))
    if routine is None:
        return
    if routine["program_id"] is not None:
        await _show_program(callback, state, routine["program_id"])
        await callback.answer()
        return
    await state.set_state(RoutineFlow.naming_program)
    await state.update_data(multiday_seed_routine_id=routine["id"])
    await ui.safe_edit(
        callback,
        "Как назвать программу целиком? (например «Мой А/Б» или «Верх-низ»)",
        reply_markup=keyboards.cancel_keyboard(f"rt:view:{routine['id']}"),
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.naming_program), F.text)
async def rt_multiday_named(message: Message, state: FSMContext):
    name, error = _valid_name(message.text)
    if error:
        await message.reply(error)
        return
    data = await state.get_data()
    routine = await _owned_routine(message, data["multiday_seed_routine_id"])
    if routine is None:
        await state.set_state(None)
        await show_manage(message, state)
        return
    program_id = await db.create_program(message.from_user.id, name, source="workout")
    if program_id is None:
        await message.reply("Программа с таким именем у тебя уже есть — придумай другое")
        return
    await db.move_routine_to_program(routine["id"], program_id)
    await state.set_state(None)
    await _show_program(message, state, program_id)


def _valid_name(raw: str) -> tuple[str | None, str | None]:
    """(имя, ошибка). Длину режем на входе: имя едет в подпись кнопки списка, и
    полотно на 500 символов разносит вёрстку того самого экрана, с которого его
    только и можно переименовать обратно. Потолок тот же, которым AI-тренер
    режет собственные названия."""
    name = raw.strip()
    if not name:
        return None, "Название не может быть пустым"
    if len(name) > config.MAX_PROGRAM_NAME_LENGTH:
        return None, f"Слишком длинное — до {config.MAX_PROGRAM_NAME_LENGTH} символов"
    return name, None


@router.callback_query(F.data.startswith("rt:rename:"))
async def rt_rename(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    await state.set_state(RoutineFlow.renaming)
    await state.update_data(routine_rename_id=routine_id)
    # «Название программы» на дне многодневки — то же слово для другого объекта:
    # переименуется день, а программа останется как была.
    what = "дня" if routine["program_id"] is not None else "программы"
    await ui.safe_edit(
        callback, f"Напиши новое название {what}:",
        reply_markup=keyboards.cancel_keyboard(f"rt:view:{routine_id}"),
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.renaming), F.text)
async def rt_rename_entered(message: Message, state: FSMContext):
    name, error = _valid_name(message.text)
    if error:
        await message.reply(error)
        return
    data = await state.get_data()
    routine_id = data["routine_rename_id"]
    await db.rename_routine(routine_id, name)
    await state.set_state(None)
    await _show_routine_detail(message, state, routine_id)


@router.callback_query(F.data.startswith("rt:pgmrename:"))
async def rt_program_rename(callback: CallbackQuery, state: FSMContext):
    """Programs recovered from old data carry a dated placeholder name (see
    db._group_program_days_saved_together) — this is how it stops being one."""
    program_id = int(callback.data.split(":")[2])
    program = await _owned_program(callback, program_id)
    if program is None:
        return
    await state.set_state(RoutineFlow.renaming_program)
    await state.update_data(program_rename_id=program_id)
    await ui.safe_edit(
        callback,
        f"Как назвать программу «{escape(program['name'])}»?",
        reply_markup=keyboards.cancel_keyboard(f"rt:prg:{program_id}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.renaming_program), F.text)
async def rt_program_rename_entered(message: Message, state: FSMContext):
    name, error = _valid_name(message.text)
    if error:
        await message.reply(error)
        return
    data = await state.get_data()
    program_id = data["program_rename_id"]
    program = await _owned_program(message, program_id)
    if program is None:
        await state.set_state(None)
        await show_manage(message, state)
        return
    if not await db.rename_program_by_id(program_id, name):
        # Раньше это было молчаливое слияние: UPDATE по program_name сливал две
        # программы в одну без единого вопроса, а разделить их обратно UI не умел.
        clash = await db.find_program_by_name(message.from_user.id, name)
        await state.update_data(program_merge_source=program_id, program_merge_target=clash["id"])
        await message.answer(
            f"Программа «{escape(name)}» у тебя уже есть. Объединить с ней "
            f"«{escape(program['name'])}» или выбрать другое имя?",
            reply_markup=keyboards.yes_no_keyboard(
                yes_cb=f"rt:pgmmerge:{program_id}:{clash['id']}",
                no_cb=f"rt:pgmrename:{program_id}",
                yes_text="🔗 Объединить", no_text="✏️ Другое имя",
            ),
            parse_mode="HTML",
        )
        return
    await state.set_state(None)
    await _show_program(message, state, program_id)


@router.callback_query(F.data.startswith("rt:pgmmerge:"))
async def rt_program_merge(callback: CallbackQuery, state: FSMContext):
    _, _, source_s, target_s = callback.data.split(":")
    source_id, target_id = int(source_s), int(target_s)
    if await _owned_program(callback, source_id) is None:
        return
    if await _owned_program(callback, target_id) is None:
        return
    await db.merge_programs(callback.from_user.id, source_id, target_id)
    await state.set_state(None)
    await callback.answer("Объединил")
    await _show_program(callback, state, target_id)


@router.callback_query(F.data.startswith("rt:pgmcopy:"))
async def rt_program_copy(callback: CallbackQuery, state: FSMContext):
    """«📄 Дублировать программу» — копия со всеми днями, составом и схемами.

    Имя берём свободное («PPL (2)»), а не спрашиваем: копию делают, чтобы
    что-то в ней поменять, и лишний экран с вводом имени стоит между решением
    и результатом. Переименовать её — соседняя кнопка.
    """
    program_id = int(callback.data.split(":")[2])
    program = await _owned_program(callback, program_id)
    if program is None:
        return
    days = await db.list_program_days_by_id(program_id)
    over_budget = await db.routine_budget(callback.from_user.id, len(days))
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return

    user_id = callback.from_user.id
    name = await db.unique_program_name(user_id, program["name"])
    copy_id = await db.create_program(
        user_id, name, source=program["source"], source_ref=program["source_ref"]
    )
    for day in days:
        day_id = await db.create_routine(user_id, day["name"], program_id=copy_id)
        for ex in await db.list_routine_exercises(day["id"]):
            await db.append_routine_exercise(day_id, ex["exercise_id"], ex["target"])
            if ex["progression"]:
                entry = (await db.list_routine_exercises(day_id))[-1]
                await db.set_routine_exercise_progression(entry["id"], ex["progression"])
    await callback.answer(f"Скопировал как «{name}»")
    await _show_program(callback, state, copy_id)


@router.callback_query(F.data.startswith("rt:pgmdelask:"))
async def rt_program_delete_confirm(callback: CallbackQuery, state: FSMContext):
    program_id = int(callback.data.split(":")[2])
    program = await _owned_program(callback, program_id)
    if program is None:
        return
    days = await db.list_program_days_by_id(program_id)
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"rt:pgmdelyes:{program_id}", no_cb=f"rt:prg:{program_id}",
        yes_text="🗑 Удалить", no_text="❌ Отмена",
    )
    word = formatting.plural_ru(len(days), ("день", "дня", "дней"))
    await ui.safe_edit(
        callback,
        f"Удалить программу «{escape(program['name'])}» целиком — все {len(days)} {word}? "
        "История тренировок не пострадает.",
        reply_markup=kb, parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:pgmdelyes:"))
async def rt_program_delete(callback: CallbackQuery, state: FSMContext):
    program_id = int(callback.data.split(":")[2])
    if await _owned_program(callback, program_id) is None:
        return
    await db.delete_program_by_id(program_id)
    await callback.answer("Программа удалена")
    await show_manage(callback, state)


@router.callback_query(F.data.startswith("rt:delask:"))
async def rt_delete_confirm(callback: CallbackQuery, state: FSMContext):
    """Удаление одного дня — и текст об этом честно говорит.

    Раньше и здесь, и на экране программы кнопка называлась «🗑 Удалить
    программу», а подтверждение — «Удалить программу?»: на дне это сносило один
    день, этажом выше — всё, и различить их было нечем.
    """
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"rt:delyes:{routine_id}", no_cb=f"rt:view:{routine_id}",
        yes_text="🗑 Удалить", no_text="❌ Отмена",
    )
    if routine["program_id"] is not None:
        text = (
            f"Удалить день «{escape(routine['name'])}» из программы "
            f"«{escape(routine['program_name'])}»? Остальные дни останутся, "
            "история тренировок не пострадает."
        )
    else:
        text = f"Удалить программу «{escape(routine['name'])}»? История тренировок не пострадает."
    await ui.safe_edit(callback, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("rt:delyes:"))
async def rt_delete(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    program_id = routine["program_id"]
    await db.delete_routine(routine_id)
    await callback.answer("День удалён" if program_id else "Программа удалена")
    # Назад туда, откуда пришли: удалив один день, ожидаешь увидеть программу без
    # него, а не весь список программ. Последний день уносит программу с собой
    # (см. db.delete_routine) — тогда возвращаться уже некуда.
    if program_id is not None and await db.get_program(program_id) is not None:
        await _show_program(callback, state, program_id)
    else:
        await show_manage(callback, state)


@router.callback_query(F.data.startswith("rt:start:"))
async def rt_start(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return

    active = await db.get_active_workout(callback.from_user.id)
    if active:
        if await db.list_exercise_ids_for_workout(active["id"]):
            # Dropping into the old session here would lose the program the user
            # just chose, and tapping "▶️ Начать" again would do the same thing —
            # a loop with no way forward. Offer the two real choices instead.
            started = dt.datetime.fromisoformat(active["started_at"])
            kb = keyboards.yes_no_keyboard(
                yes_cb=f"rt:finishprev:{routine_id}",
                no_cb=f"rt:resumeprev:{routine_id}",
                yes_text="🏁 Завершить и начать",
                no_text="↩️ Вернуться к ней",
            )
            await ui.safe_edit(
                callback,
                f"У тебя не закрыта тренировка от <b>{formatting.format_date_ru(started)}</b>.\n"
                f"Завершить её и начать по программе «{escape(routine['name'])}»?",
                reply_markup=kb,
                parse_mode="HTML",
            )
            await callback.answer()
            return
        await db.discard_workout(active["id"])

    await _begin_routine_workout(callback, state, routine)
    await callback.answer()


async def _begin_routine_workout(callback: CallbackQuery, state: FSMContext, routine) -> None:
    """Create the workout and load the routine's first block. Assumes any
    previously active workout has already been dealt with."""
    from handlers.workout import _delete_message as wk_delete
    from handlers.workout import _load_next_planned_block, _picker_screen_groups, _reset_new_workout_scaffold

    exercises = await db.list_routine_exercises(routine["id"])
    planned = [
        {"exercise_ids": [ex["exercise_id"]], "targets": {ex["exercise_id"]: ex["target"]}}
        for ex in exercises
    ]

    await _reset_new_workout_scaffold(state)
    # routine_id: единственный след того, что тренировка шла по программе — сам
    # план живёт в FSM и исчезает вместе с ней. По нему экран старта показывает
    # программы, по которым человек ходит в последнее время.
    workout_id = await db.create_workout(callback.from_user.id, routine_id=routine["id"])
    await wk_delete(callback.message)
    sent = await callback.message.answer(f"🏋️ Тренировка по программе «{routine['name']}»")
    await state.update_data(
        workout_id=workout_id, live_chat_id=sent.chat.id, live_message_id=sent.message_id,
        last_by_exercise={}, planned_blocks=planned,
    )
    if planned:
        await _load_next_planned_block(callback, state)
    else:
        await state.set_state(WorkoutFlow.picking_group)
        await _picker_screen_groups(callback, state)


@router.callback_query(F.data.startswith("rt:finishprev:"))
async def rt_finish_previous_and_start(callback: CallbackQuery, state: FSMContext):
    """"🏁 Завершить и начать" — close the stale session (keeping what's in it)
    and start the chosen program straight away."""
    routine_id = int(callback.data.split(":")[2])
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    active = await db.get_active_workout(callback.from_user.id)
    if active:
        await db.delete_empty_blocks(active["id"])
        await db.finish_workout(active["id"])
    await _begin_routine_workout(callback, state, routine)
    await callback.answer("Прошлая тренировка завершена")


@router.callback_query(F.data.startswith("rt:resumeprev:"))
async def rt_resume_previous(callback: CallbackQuery, state: FSMContext):
    """"↩️ Вернуться к ней" — the other half of the same choice."""
    from handlers.workout import _enter_live

    active = await db.get_active_workout(callback.from_user.id)
    if active is None:
        await callback.answer("Активной тренировки уже нет")
        await _show_routine_detail(callback, state, int(callback.data.split(":")[2]))
        return
    await _enter_live(callback, state, active["id"])
    await callback.answer()


# ---------- days of a program: add, copy, reorder, take out ----------
#
# All four were impossible while a program was just the string its days shared:
# there was nothing to add a day *to*, order was implied by ascending id, and a
# day that landed in a program stayed there. A catalog three-day split could
# never grow a fourth day for arms — you rebuilt the whole thing.


@router.callback_query(F.data.startswith("rt:dayadd:"))
async def rt_day_add(callback: CallbackQuery, state: FSMContext):
    program_id = int(callback.data.split(":")[2])
    if await _owned_program(callback, program_id) is None:
        return
    over_budget = await db.routine_budget(callback.from_user.id, 1)
    if over_budget:
        await callback.answer(over_budget, show_alert=True)
        return
    days = await db.list_program_days_by_id(program_id)
    await ui.safe_edit(
        callback, "Какой день добавить?",
        reply_markup=keyboards.program_day_source_keyboard(program_id, days),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:dayblank:"))
async def rt_day_blank(callback: CallbackQuery, state: FSMContext):
    program_id = int(callback.data.split(":")[2])
    if await _owned_program(callback, program_id) is None:
        return
    await state.set_state(RoutineFlow.naming_day)
    await state.update_data(day_program_id=program_id, day_copy_from=None)
    await ui.safe_edit(
        callback, "Как назвать день? (например «Руки» или «День 4»)",
        reply_markup=keyboards.cancel_keyboard(f"rt:prg:{program_id}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:daycopy:"))
async def rt_day_copy(callback: CallbackQuery, state: FSMContext):
    """Копия существующего дня — база для «то же самое, но с другой грудью»."""
    source_id = int(callback.data.split(":")[2])
    source = await _owned_routine(callback, source_id)
    if source is None or source["program_id"] is None:
        return
    await state.set_state(RoutineFlow.naming_day)
    await state.update_data(day_program_id=source["program_id"], day_copy_from=source_id)
    await ui.safe_edit(
        callback, f"Как назвать копию дня «{escape(source['name'])}»?",
        reply_markup=keyboards.cancel_keyboard(f"rt:prg:{source['program_id']}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(RoutineFlow.naming_day), F.text)
async def rt_day_named(message: Message, state: FSMContext):
    name, error = _valid_name(message.text)
    if error:
        await message.reply(error)
        return
    data = await state.get_data()
    program_id = data["day_program_id"]
    program = await _owned_program(message, program_id)
    if program is None:
        await state.set_state(None)
        await show_manage(message, state)
        return
    routine_id = await db.create_routine(message.from_user.id, name, program_id=program_id)
    source_id = data.get("day_copy_from")
    if source_id is not None:
        for ex in await db.list_routine_exercises(source_id):
            await db.append_routine_exercise(routine_id, ex["exercise_id"], ex["target"])
    await state.set_state(None)
    await _show_routine_detail(message, state, routine_id)


@router.callback_query(F.data.startswith("rt:dayorder:"))
async def rt_day_order(callback: CallbackQuery, state: FSMContext):
    program_id = int(callback.data.split(":")[2])
    if await _owned_program(callback, program_id) is None:
        return
    days = await db.list_program_days_by_id(program_id)
    await ui.safe_edit(
        callback, "Порядок дней — стрелками:",
        reply_markup=keyboards.program_day_order_keyboard(days, program_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:daymv:"))
async def rt_day_move(callback: CallbackQuery, state: FSMContext):
    _, _, routine_id_s, direction = callback.data.split(":")
    routine = await _owned_routine(callback, int(routine_id_s))
    if routine is None or routine["program_id"] is None:
        return
    await db.reorder_program_day(routine["id"], direction)
    days = await db.list_program_days_by_id(routine["program_id"])
    await ui.safe_edit(
        callback, "Порядок дней — стрелками:",
        reply_markup=keyboards.program_day_order_keyboard(days, routine["program_id"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:dayout:"))
async def rt_day_out(callback: CallbackQuery, state: FSMContext):
    """«Вынести из программы» — день становится одиночной программой."""
    routine = await _owned_routine(callback, int(callback.data.split(":")[2]))
    if routine is None:
        return
    if routine["program_id"] is None:
        await callback.answer("Он и так сам по себе")
        return
    await db.move_routine_to_program(routine["id"], None)
    await callback.answer("Вынес из программы")
    await _show_routine_detail(callback, state, routine["id"])


# ---------- editing a saved routine's exercise list ----------
#
# Adding reuses the same shape as the live-workout picker (groups → exercises,
# with search and template forking) — a different prefix ("rtadd") targeting
# db.append_routine_exercise instead of opening a live block. Removing is a
# single tap with no confirmation: unlike deleting the whole program, dropping
# one exercise is trivially undone with "➕ Добавить упражнение".

@router.callback_query(F.data.startswith("rt:rmex:"))
async def rt_remove_exercise_confirm(callback: CallbackQuery, state: FSMContext):
    """Одна лишняя опечатка-тап в списке — и упражнение вон из программы
    навсегда (в отличие от подхода в тренировке, тут нет "отменить"), так что
    сначала спрашиваем, а сам снос делает rt_remove_exercise ниже."""
    _, _, routine_id_s, re_id_s = callback.data.split(":")
    routine_id, re_id = int(routine_id_s), int(re_id_s)
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    entry = await db.get_routine_exercise(re_id)
    if entry is None or entry["routine_id"] != routine_id:
        await callback.answer("Упражнение уже убрано", show_alert=True)
        await _show_routine_editor(callback, state, routine_id)
        return
    exercise = await db.get_exercise(entry["exercise_id"])
    name = exercise["display_name"] if exercise else "упражнение"
    kb = keyboards.yes_no_keyboard(
        yes_cb=f"rt:rmexyes:{routine_id}:{re_id}", no_cb=f"rt:edit:{routine_id}",
        yes_text="🗑 Убрать", no_text="❌ Отмена",
    )
    await ui.safe_edit(
        callback, f"Убрать «{escape(name)}» из программы?", reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rt:rmexyes:"))
async def rt_remove_exercise(callback: CallbackQuery, state: FSMContext):
    _, _, routine_id_s, re_id_s = callback.data.split(":")
    routine_id, re_id = int(routine_id_s), int(re_id_s)
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    entry = await db.get_routine_exercise(re_id)
    if entry is None or entry["routine_id"] != routine_id:
        await callback.answer("Упражнение уже убрано", show_alert=True)
        await _show_routine_editor(callback, state, routine_id)
        return
    await db.remove_routine_exercise(re_id)
    await callback.answer("Убрал из программы")
    await _show_routine_editor(callback, state, routine_id)


@router.callback_query(F.data.startswith("rt:extarget:"))
async def rt_edit_exercise_target(callback: CallbackQuery, state: FSMContext):
    """✏️ рядом с упражнением — поменять схему подходов, не трогая позицию.

    До этого «3×10» → «4×8» стоило до девяти тапов: убрать (с подтверждением),
    добавить заново, упражнение уезжало в конец, поднимать стрелками.
    """
    _, _, routine_id_s, re_id_s = callback.data.split(":")
    routine_id, re_id = int(routine_id_s), int(re_id_s)
    if await _owned_routine(callback, routine_id) is None:
        return
    entry = await db.get_routine_exercise(re_id)
    if entry is None or entry["routine_id"] != routine_id:
        await callback.answer("Упражнение уже убрано", show_alert=True)
        await _show_routine_editor(callback, state, routine_id)
        return
    exercise = await db.get_exercise(entry["exercise_id"])
    name = exercise["display_name"] if exercise else "упражнение"
    await state.set_state(RoutineFlow.editing_exercise_target)
    await state.update_data(rtedit_routine_id=routine_id, rtedit_re_id=re_id)
    current = f"\nСейчас: {escape(entry['target'])}" if entry["target"] else ""
    await ui.safe_edit(
        callback,
        f"Схема подходов для «{escape(name)}»? Например «3x8-12».{current}",
        reply_markup=keyboards.routine_exercise_target_keyboard("rt:extclear"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.editing_exercise_target), F.data == "rt:extclear")
async def rt_clear_exercise_target(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await db.set_routine_exercise_target(data["rtedit_re_id"], None)
    await state.set_state(None)
    await _show_routine_editor(callback, state, data["rtedit_routine_id"])
    await callback.answer("Убрал схему")


@router.message(StateFilter(RoutineFlow.editing_exercise_target), F.text)
async def rt_exercise_target_entered(message: Message, state: FSMContext):
    target = message.text.strip()
    if not target:
        return
    data = await state.get_data()
    await db.set_routine_exercise_target(data["rtedit_re_id"], target)
    await state.set_state(None)
    await _show_routine_editor(message, state, data["rtedit_routine_id"])


@router.callback_query(F.data.startswith("rt:mvex:"))
async def rt_move_exercise(callback: CallbackQuery, state: FSMContext):
    _, _, routine_id_s, re_id_s, direction = callback.data.split(":")
    routine_id, re_id = int(routine_id_s), int(re_id_s)
    routine = await _owned_routine(callback, routine_id)
    if routine is None:
        return
    await db.reorder_routine_exercise(re_id, direction)
    await _show_routine_editor(callback, state, routine_id)
    await callback.answer()


async def _rtadd_groups_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RoutineFlow.adding_exercise_group)
    groups = await db.list_muscle_groups(callback.from_user.id)
    kb = keyboards.groups_keyboard(
        groups, prefix="rtadd", extra_buttons=[("❌ Отмена", "rtadd:cancel")], show_all=True
    )
    await ui.safe_edit(
        callback, "Выбери группу мышц или найди упражнение по названию:", reply_markup=kb
    )


@router.callback_query(F.data.startswith("rt:addex:"))
async def rt_add_exercise_start(callback: CallbackQuery, state: FSMContext):
    routine_id = int(callback.data.split(":")[2])
    if await _owned_routine(callback, routine_id) is None:
        return
    await state.update_data(rtadd_routine_id=routine_id, rtadd_group_id=None, rtadd_page=0)
    await _rtadd_groups_screen(callback, state)
    await callback.answer()


@router.callback_query(
    StateFilter(
        RoutineFlow.adding_exercise_group,
        RoutineFlow.adding_exercise_pick,
        RoutineFlow.adding_exercise_target,
    ),
    F.data == "rtadd:cancel",
)
async def rtadd_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    routine_id = data.get("rtadd_routine_id")
    await state.set_state(None)
    if routine_id is not None:
        await _show_routine_editor(callback, state, routine_id)
    await callback.answer()


async def _rtadd_exercise_list_screen(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RoutineFlow.adding_exercise_pick)
    data = await state.get_data()
    group_id = data.get("rtadd_group_id")
    page = data.get("rtadd_page", 0)
    offset = page * config.RECENT_EXERCISES_LIMIT
    if group_id is None:
        exercises = await db.list_user_exercises(
            callback.from_user.id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises(callback.from_user.id)
    else:
        exercises = await db.list_user_exercises_in_group(
            callback.from_user.id, group_id, limit=config.RECENT_EXERCISES_LIMIT, offset=offset
        )
        total = await db.count_user_exercises_in_group(callback.from_user.id, group_id)
    has_next = offset + len(exercises) < total
    kb = keyboards.exercises_keyboard(
        exercises, prefix="rtadd", show_new_button=False, back_cb="back", page=page, has_next=has_next,
        show_catalog_button=group_id is not None,
    )
    text = "Выбери упражнение или напиши название для поиска:" if exercises else "Пусто здесь — напиши название для поиска."
    await ui.safe_edit(callback, text, reply_markup=kb)


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_group), F.data.startswith("rtadd:grp:"))
async def rtadd_pick_group(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":")[2]
    group_id = None if raw == "all" else int(raw)
    await state.update_data(rtadd_group_id=group_id, rtadd_page=0)
    await _rtadd_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data.startswith("rtadd:page:"))
async def rtadd_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await state.update_data(rtadd_page=page)
    await _rtadd_exercise_list_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data == "rtadd:back")
async def rtadd_back_to_groups(callback: CallbackQuery, state: FSMContext):
    await _rtadd_groups_screen(callback, state)
    await callback.answer()


async def _rtadd_catalog_screen(callback: CallbackQuery, state: FSMContext) -> None:
    """Каталог упражнений — шаблоны выбранной группы, а не только свои: до
    этого экрана шаблон можно было добавить лишь угадав его название в поиске."""
    data = await state.get_data()
    group_id = data.get("rtadd_group_id")
    templates = await db.list_templates_in_group(group_id) if group_id is not None else []
    b = InlineKeyboardBuilder()
    for t in templates:
        b.row(InlineKeyboardButton(text=t["display_name"], callback_data=f"rtadd:tpladd:{t['id']}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rtadd:catalogback"))
    text = "📋 Шаблоны — выбери подходящий:" if templates else "В этой группе шаблонов нет."
    await ui.safe_edit(callback, text, reply_markup=b.as_markup())


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data == "rtadd:catalog")
async def rtadd_catalog(callback: CallbackQuery, state: FSMContext):
    await _rtadd_catalog_screen(callback, state)
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data == "rtadd:catalogback")
async def rtadd_catalog_back(callback: CallbackQuery, state: FSMContext):
    await _rtadd_exercise_list_screen(callback, state)
    await callback.answer()


async def _rtadd_finish(event, state: FSMContext, exercise_id: int, target: str | None = None) -> None:
    data = await state.get_data()
    routine_id = data["rtadd_routine_id"]
    # Дедуп, которого тут не было: и create_routine_from_program, и
    # create_routine_from_workout держат `seen`, а ручное добавление — нет.
    # Дубль потом даёт два одинаковых пункта в плане тренировки, а ручной выбор
    # снимает с плана оба сразу (см. workout._drop_planned_exercise).
    existing = [ex for ex in await db.list_routine_exercises(routine_id)
                if ex["exercise_id"] == exercise_id]
    if existing:
        await state.set_state(None)
        await _show_routine_editor(event, state, routine_id)
        if isinstance(event, CallbackQuery):
            await event.answer("Оно уже здесь — схему можно поменять ✏️", show_alert=True)
        return
    await db.append_routine_exercise(routine_id, exercise_id, target)
    await db.touch_exercise_last_used(exercise_id)
    await state.set_state(None)
    # Back to the composition editor the "➕" was tapped from, so several
    # exercises can be added in a row.
    await _show_routine_editor(event, state, routine_id)
    if isinstance(event, CallbackQuery):
        await event.answer("Добавил в программу")


async def _rtadd_ask_target(callback: CallbackQuery, state: FSMContext, exercise_id: int) -> None:
    """After picking what to add, ask for a sets/reps scheme ("3x8-12") before
    it lands in the program — skippable, since not everyone tracks one."""
    await state.update_data(rtadd_exercise_id=exercise_id)
    await state.set_state(RoutineFlow.adding_exercise_target)
    await ui.safe_edit(
        callback,
        "Схема сетов/повторов для этого упражнения? Например «3x8-12». "
        "Или нажми «Пропустить».",
        reply_markup=keyboards.routine_exercise_target_keyboard("rtadd:notarget"),
    )
    await callback.answer()


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_pick), F.data.startswith("rtadd:ex:"))
async def rtadd_pick_exercise(callback: CallbackQuery, state: FSMContext):
    ex_id = int(callback.data.split(":")[2])
    await _rtadd_ask_target(callback, state, ex_id)


@router.callback_query(
    StateFilter(RoutineFlow.adding_exercise_group, RoutineFlow.adding_exercise_pick),
    F.data.startswith("rtadd:tpladd:"),
)
async def rtadd_pick_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[2])
    ex_id = await db.fork_exercise_from_template(callback.from_user.id, template_id)
    await _rtadd_ask_target(callback, state, ex_id)


@router.callback_query(StateFilter(RoutineFlow.adding_exercise_target), F.data == "rtadd:notarget")
async def rtadd_skip_target(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _rtadd_finish(callback, state, data["rtadd_exercise_id"])


@router.message(StateFilter(RoutineFlow.adding_exercise_target), F.text)
async def rtadd_target_entered(message: Message, state: FSMContext):
    target = message.text.strip()
    if not target:
        return
    data = await state.get_data()
    await _rtadd_finish(message, state, data["rtadd_exercise_id"], target)


@router.message(StateFilter(RoutineFlow.adding_exercise_group, RoutineFlow.adding_exercise_pick), F.text)
async def rtadd_search_text(message: Message, state: FSMContext):
    """Typing while picking what to add searches — own exercises plus catalog
    templates (forked on tap), same merge as the live-workout picker."""
    query = message.text.strip()
    if not query:
        return
    results = await db.search_exercises(message.from_user.id, query)
    templates = await db.search_exercise_templates(message.from_user.id, query)
    kb = keyboards.exercises_keyboard(results, prefix="rtadd", show_new_button=False, templates=templates)
    if results or templates:
        text = f"Результаты поиска «{escape(query)}»:"
    else:
        text = f"Ничего не нашлось по «{escape(query)}»."
    await state.set_state(RoutineFlow.adding_exercise_pick)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
