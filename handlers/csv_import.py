"""§A3 — CSV import (round-trip with the §9 export): дата, упражнение, вес, повторы[, подход]."""

import csv
import datetime as dt
import io

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import db
import formatting
import keyboards
import ui
from fsm import ImportFlow
from parser import ParseError, parse_ru_date

router = Router(name="csv_import")

REQUIRED_FIELDS = ["date", "exercise", "weight", "reps"]
FIELD_LABELS = {"date": "дата", "exercise": "упражнение", "weight": "вес", "reps": "повторы", "round": "номер подхода"}
SYNONYMS = {
    "date": {"дата", "date", "started_at"},
    "exercise": {"упражнение", "exercise"},
    "weight": {"вес", "weight"},
    "reps": {"повторы", "reps"},
    "round": {"подход", "раунд", "round", "set", "round_index"},
}


@router.callback_query(F.data == "settings:import")
async def import_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ImportFlow.awaiting_file)
    await ui.safe_edit(
        callback,
        "📥 Пришли CSV-файл с колонками «дата, упражнение, вес, повторы» "
        "(можно подойдёт экспорт из этого бота).",
        reply_markup=keyboards.cancel_keyboard("imp:cancel"),
    )
    await callback.answer()


def _auto_detect(headers: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in headers]
    mapping: dict[str, int] = {}
    for field, names in SYNONYMS.items():
        for idx, h in enumerate(lowered):
            if h in names:
                mapping[field] = idx
                break
    return mapping


async def _ask_next_mapping(event, state: FSMContext) -> bool:
    """Returns True if a mapping question was asked, False if mapping is complete."""
    data = await state.get_data()
    pending = list(data.get("imp_pending_fields") or [])
    if not pending:
        return False
    field = pending[0]
    headers = data["imp_headers"]
    await state.set_state(ImportFlow.mapping_columns)
    kb = keyboards.csv_column_options_keyboard(headers, prefix=f"impcol:{field}")
    text = f"Какая колонка соответствует полю «{FIELD_LABELS[field]}»?\nКолонки файла: {', '.join(headers)}"
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)
    return True


@router.message(StateFilter(ImportFlow.awaiting_file), F.document)
async def import_file_received(message: Message, state: FSMContext):
    document = message.document
    if not document.file_name.lower().endswith(".csv"):
        await message.reply("Нужен файл с расширением .csv")
        return
    buf = await message.bot.download(document)
    raw = buf.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        await message.reply("Файл пустой.")
        return
    headers, data_rows = rows[0], rows[1:]
    if not data_rows:
        await message.reply("В файле нет строк с данными.")
        return

    mapping = _auto_detect(headers)
    pending = [f for f in REQUIRED_FIELDS if f not in mapping]
    await state.update_data(
        imp_headers=headers, imp_rows=data_rows, imp_mapping=mapping, imp_pending_fields=pending,
    )
    if not await _ask_next_mapping(message, state):
        await _finish_mapping(message, state)


@router.message(StateFilter(ImportFlow.awaiting_file))
async def import_file_missing(message: Message, state: FSMContext):
    await message.reply("Пришли CSV-файл документом (не текстом).")


@router.callback_query(StateFilter(ImportFlow.mapping_columns), F.data.startswith("impcol:"))
async def import_column_picked(callback: CallbackQuery, state: FSMContext):
    _, field, idx_str = callback.data.split(":")
    data = await state.get_data()
    mapping = dict(data["imp_mapping"])
    mapping[field] = int(idx_str)
    pending = [f for f in data.get("imp_pending_fields") or [] if f != field]
    await state.update_data(imp_mapping=mapping, imp_pending_fields=pending)
    if not await _ask_next_mapping(callback, state):
        await _finish_mapping(callback, state)
    await callback.answer()


def _parse_row_date(text: str) -> dt.date:
    text = text.strip()
    try:
        return parse_ru_date(text)
    except ParseError:
        pass
    try:
        if "t" in text.lower():
            return dt.datetime.fromisoformat(text).date()
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        raise ParseError(f"не понял дату «{text}»") from None


def _build_workout_groups(rows: list[list[str]], mapping: dict[str, int]) -> list[dict]:
    groups: dict[str, dict[str, list[tuple]]] = {}
    name_order: dict[str, list[str]] = {}
    date_order: list[str] = []

    for line_no, row in enumerate(rows, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        try:
            date_val = _parse_row_date(row[mapping["date"]])
            name = row[mapping["exercise"]].strip()
            weight_text = row[mapping["weight"]].strip()
            weight = float(weight_text.replace(",", ".")) if weight_text else 0.0
            reps = int(row[mapping["reps"]].strip())
            round_val = None
            if "round" in mapping:
                round_text = row[mapping["round"]].strip()
                round_val = int(round_text) if round_text else None
        except ParseError as e:
            raise ParseError(f"Строка {line_no}: {e.message}") from None
        except (ValueError, IndexError):
            raise ParseError(f"Строка {line_no}: не разобрал вес/повторы") from None

        if not name:
            raise ParseError(f"Строка {line_no}: пустое название упражнения")
        if reps <= 0:
            raise ParseError(f"Строка {line_no}: повторы должны быть больше 0")

        date_iso = date_val.isoformat()
        if date_iso not in groups:
            groups[date_iso] = {}
            name_order[date_iso] = []
            date_order.append(date_iso)
        if name not in groups[date_iso]:
            groups[date_iso][name] = []
            name_order[date_iso].append(name)
        groups[date_iso][name].append((round_val, weight, reps))

    workouts = []
    for date_iso in date_order:
        entries = []
        for name in name_order[date_iso]:
            rows_for_ex = groups[date_iso][name]
            if all(r[0] is not None for r in rows_for_ex):
                rows_for_ex = sorted(rows_for_ex, key=lambda r: r[0])
            entries.append({"name": name, "sets": [[w, r] for _, w, r in rows_for_ex]})
        workouts.append({"date": date_iso, "entries": entries})
    return workouts


async def _finish_mapping(event, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        workouts = _build_workout_groups(data["imp_rows"], data["imp_mapping"])
    except ParseError as e:
        text = f"Ошибка в файле: {e.message}\nИсправь файл и пришли заново."
        await state.set_state(ImportFlow.awaiting_file)
        kb = keyboards.cancel_keyboard("imp:cancel")
        if isinstance(event, CallbackQuery):
            await ui.safe_edit(event, text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)
        return

    await state.update_data(imp_workouts=workouts, imp_resolved={})
    all_names = [entry["name"] for w in workouts for entry in w["entries"]]
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    user_id = event.from_user.id
    for name in dict.fromkeys(all_names):
        ex = await db.find_exercise_by_name(user_id, name)
        if ex:
            resolved[name] = ex["id"]
        else:
            unresolved.append(name)
    await state.update_data(imp_resolved=resolved)

    if unresolved:
        from handlers.exercise_resolve import start as start_resolve
        await start_resolve(event, state, unresolved)
    else:
        await show_confirmation(event, state)


async def on_exercises_resolved(event, state: FSMContext) -> None:
    data = await state.get_data()
    resolved = dict(data.get("imp_resolved") or {})
    resolved.update(data.get("resolve_resolved") or {})
    await state.update_data(imp_resolved=resolved)
    await show_confirmation(event, state)


async def show_confirmation(event, state: FSMContext) -> None:
    data = await state.get_data()
    workouts = data["imp_workouts"]
    total_sets = sum(len(entry["sets"]) for w in workouts for entry in w["entries"])
    total_exercises = sum(len(w["entries"]) for w in workouts)
    dates = ", ".join(formatting.format_date_ru(dt.date.fromisoformat(w["date"])) for w in workouts[:10])
    if len(workouts) > 10:
        dates += f" и ещё {len(workouts) - 10}"

    text = (
        f"📋 Готово к импорту: {len(workouts)} тренировки ({dates}), "
        f"{total_exercises} упражнения, {total_sets} сетов.\nЗагрузить?"
    )
    await state.set_state(ImportFlow.confirming)
    kb = keyboards.confirm_cancel_keyboard("imp:save", "imp:cancel", confirm_text="✅ Загрузить")
    if isinstance(event, CallbackQuery):
        await ui.safe_edit(event, text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(StateFilter(ImportFlow.confirming), F.data == "imp:save")
async def import_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    workouts = data["imp_workouts"]
    resolved = data["imp_resolved"]
    user_id = callback.from_user.id

    for w in workouts:
        started_at = f"{w['date']}T12:00:00"
        workout_id = await db.create_finished_workout(user_id, started_at, started_at, source="import")
        for entry in w["entries"]:
            ex_id = resolved[entry["name"]]
            block_id = await db.create_block(workout_id, "single")
            await db.add_block_exercise(block_id, ex_id, 0)
            await db.touch_exercise_last_used(ex_id)
            for idx, (weight, reps) in enumerate(entry["sets"], start=1):
                await db.add_set(block_id, ex_id, idx, 0, weight, reps)

    await state.clear()
    await ui.safe_edit(callback, f"✅ Импортировано {len(workouts)} тренировок.")
    from handlers.settings import show_settings
    await show_settings(callback, state)
    await callback.answer()


@router.callback_query(F.data == "imp:cancel")
async def import_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    from handlers.settings import show_settings
    await show_settings(callback, state)
    await callback.answer("Отменено")
