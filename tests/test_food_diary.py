"""Дневник питания: слой БД, разбор ответа модели, тексты экранов и сам флоу."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import ai_trainer
import db as dbmod
import formatting
import keyboards
from fsm import FoodDiaryFlow
from handlers import food_diary

# pytest.ini ставит asyncio_mode = auto — async-тесты подхватываются сами, а
# файл смешанный (чистые форматтеры/клавиатуры синхронные).


# ---------- db ----------


async def test_food_entry_roundtrip_and_day_grouping(user_id):
    await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350, protein=12)
    await dbmod.add_food_entry(user_id, "2026-07-20", "Кофе с молоком", calories=60)
    await dbmod.add_food_entry(user_id, "2026-07-21", "Творог", calories=200)

    day = await dbmod.list_food_entries(user_id, "2026-07-20")
    assert [r["description"] for r in day] == ["Овсянка", "Кофе с молоком"]  # в порядке ввода
    assert day[0]["protein"] == 12
    assert day[0]["source"] == "text"

    assert len(await dbmod.list_food_entries(user_id, "2026-07-21")) == 1
    assert await dbmod.list_food_entries(user_id, "2026-07-22") == []


async def test_food_days_history_newest_first_with_totals(user_id):
    await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350, protein=12, fat=8, carbs=55)
    await dbmod.add_food_entry(user_id, "2026-07-20", "Кофе", calories=60, protein=1)
    await dbmod.add_food_entry(user_id, "2026-07-21", "Творог", calories=200, protein=20, fat=5, carbs=10)

    days = await dbmod.list_food_days(user_id)
    assert [r["eaten_on"] for r in days] == ["2026-07-21", "2026-07-20"]
    assert [r["entries"] for r in days] == [1, 2]
    assert [r["calories"] for r in days] == [200, 410]
    assert [r["protein"] for r in days] == [20, 13]
    assert days[1]["fat"] == 8  # у второй записи (Кофе) fat не задан — суммируем только известное
    # список названий за день — в порядке ввода, не алфавитном/произвольном
    assert days[1]["descriptions"] == "Овсянка\nКофе"
    assert days[0]["descriptions"] == "Творог"
    assert await dbmod.count_food_days(user_id) == 2


async def test_food_days_paginate(user_id):
    for day in range(1, 6):
        await dbmod.add_food_entry(user_id, f"2026-07-0{day}", "Еда", calories=100)
    page = await dbmod.list_food_days(user_id, limit=2, offset=2)
    assert [r["eaten_on"] for r in page] == ["2026-07-03", "2026-07-02"]


async def test_delete_food_entry(user_id):
    entry_id = await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)
    assert (await dbmod.get_food_entry(entry_id))["description"] == "Овсянка"
    await dbmod.delete_food_entry(entry_id)
    assert await dbmod.get_food_entry(entry_id) is None
    assert await dbmod.list_food_entries(user_id, "2026-07-20") == []


async def test_food_entry_keeps_nulls_when_model_gave_no_numbers(user_id):
    entry_id = await dbmod.add_food_entry(user_id, "2026-07-20", "Что-то съел")
    entry = await dbmod.get_food_entry(entry_id)
    assert entry["calories"] is None and entry["protein"] is None
    assert entry["photo_file_id"] is None


# ---------- разбор ответа модели ----------


def test_extract_json_object_survives_markdown_fence():
    data = ai_trainer._extract_json_object('```json\n{"description": "Овсянка"}\n```')
    assert data["description"] == "Овсянка"


def test_extract_json_object_survives_surrounding_prose():
    data = ai_trainer._extract_json_object('Вот оценка: {"calories": 420} — примерно так')
    assert data["calories"] == 420


def test_extract_json_object_raises_without_json():
    with pytest.raises(ValueError):
        ai_trainer._extract_json_object("вообще не JSON")


@pytest.mark.parametrize(
    "raw,expected",
    [(420, 420.0), (12.5, 12.5), ("420 ккал", 420.0), ("~15,5 г", 15.5), (None, None), ("нет", None), (True, None)],
)
def test_as_number(raw, expected):
    assert ai_trainer._as_number(raw) == expected


def test_atwater_kcal_needs_the_full_set():
    assert ai_trainer._atwater_kcal({"protein": 28, "fat": 32, "carbs": 48}) == 592.0
    assert ai_trainer._atwater_kcal({"protein": 28, "fat": None, "carbs": 48}) is None


def test_reconcile_rounds_grams_then_derives_kcal():
    """Округление до целых граммов идёт ДО пересчёта, иначе показанные «28 г»
    и показанные ккал снова разойдутся."""
    entry = {"calories": 620.0, "protein": 28.4, "fat": 31.6, "carbs": 47.7}
    ai_trainer._reconcile_macros(entry)
    assert (entry["protein"], entry["fat"], entry["carbs"]) == (28.0, 32.0, 48.0)
    assert entry["calories"] == 28 * 4 + 32 * 9 + 48 * 4


def test_reconcile_leaves_incomplete_macros_alone():
    entry = {"calories": 250.0, "protein": 20.0, "fat": None, "carbs": 10.0}
    ai_trainer._reconcile_macros(entry)
    assert entry["calories"] == 250.0  # пересчитывать не из чего


# ---------- тексты экранов ----------


def _view(**kwargs) -> formatting.FoodEntryView:
    base = {"id": 1, "description": "Овсянка"}
    base.update(kwargs)
    return formatting.FoodEntryView(**base)


def _item(**kwargs) -> formatting.FoodItemView:
    base = {"name": "Овсянка"}
    base.update(kwargs)
    return formatting.FoodItemView(**base)


def test_day_screen_empty_explains_how_to_add():
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), [])
    assert "20.07.2026" in text
    assert "пришли фото" in text


def test_day_screen_numbers_entries_and_totals():
    entries = [
        _view(id=1, description="Овсянка", calories=350, protein=12, fat=8, carbs=55),
        _view(id=2, description="Кофе", calories=60, has_photo=True),
    ]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    assert "1. Овсянка" in text
    assert "2. Кофе" in text
    assert "📷" in text
    # итог дня — ккал и БЖУ жирным одной строкой, число приёмов в скобках рядом
    assert "Итого за день: <b>410 ккал · Б12 · Ж8 · У55</b> (2 приёма пищи)" in text
    # подсказка внизу — без точки на конце
    assert text.rstrip().endswith("добавлю новой записью</i>")


def test_day_screen_last_entry_sits_right_above_the_divider():
    """Пустая строка разделяет приёмы между собой, но перед чертой-разделителем
    её быть не должно — последний приём идёт к ней вплотную."""
    entries = [
        _view(id=1, description="Овсянка", calories=350),
        _view(id=2, description="Кофе", calories=60),
    ]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    assert "350 ккал</b>\n\n<b>2. Кофе" in text  # между приёмами — пустая строка
    assert f"60 ккал</b>\n{formatting.DIVIDER}" in text  # а перед чертой — нет


def test_non_empty_day_screen_still_hints_at_adding_more():
    """Без этой строки экран после первой записи выглядит тупиком — непонятно,
    что можно просто дописать ещё один приём пищи (см. отчёт пользователя)."""
    text = formatting.build_food_day_screen(
        dt.date(2026, 7, 20), [_view(id=1, description="Овсянка", calories=350)]
    )
    assert "Напиши текстом или пришли фото" in text


def test_day_screen_flags_entries_without_calories():
    entries = [_view(id=1, calories=350), _view(id=2, description="Чай")]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    assert "350 ккал" in text
    assert "без калорий: 1" in text


def test_day_screen_escapes_html_in_description():
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), [_view(description="<b>хак</b>")])
    assert "<b>хак</b>" not in text
    assert "&lt;b&gt;" in text


def test_day_screen_shows_per_item_macros():
    entries = [
        _view(
            id=1, description="Гранола с протеином", calories=750, protein=39, fat=24, carbs=101,
            items=[
                _item(name="Протеин", portion="30 г", calories=120, protein=24, fat=1, carbs=3),
                _item(name="Гранола", portion="150 г", calories=630, protein=15, fat=23, carbs=98),
            ],
        )
    ]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    # у отдельных продуктов — обычным текстом, не перегружаем скобки
    assert "Протеин — 30 г — 120 ккал (Б24 · Ж1 · У3)" in text
    assert "Гранола — 150 г — 630 ккал (Б15 · Ж23 · У98)" in text
    # итог по приёму — ккал и БЖУ одной строкой под раскладкой, жирным
    assert "<b>750 ккал · Б39 · Ж24 · У101</b>" in text
    # калорий блюда больше нет при названии наверху
    assert "Гранола с протеином</b> — 750 ккал" not in text
    # больше одного компонента — раскладка под сворачиваемой цитатой
    assert "<blockquote expandable>" in text


def test_day_screen_skips_breakdown_for_a_single_item():
    """Один компонент ничего не добавляет к названию приёма — не дублируем,
    и заворачивать в сворачиваемую цитату тоже нечего."""
    entries = [
        _view(
            id=1, description="Кофе", calories=60, protein=1, fat=1, carbs=8,
            items=[_item(name="Кофе", portion="200 мл", calories=60, protein=1, fat=1, carbs=8)],
        )
    ]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    assert "Кофе — 200 мл" not in text
    assert "<blockquote" not in text
    assert "<b>60 ккал · Б1 · Ж1 · У8</b>" in text


def test_day_screen_entry_totals_omit_missing_half():
    """Только ккал (без БЖУ) или только БЖУ (без ккал) — итоговая строка не
    показывает пустое место вместо недостающей половины."""
    only_kcal = formatting.build_food_day_screen(
        dt.date(2026, 7, 20), [_view(id=1, description="Чай", calories=40)]
    )
    assert "<b>40 ккал</b>" in only_kcal

    only_macros = formatting.build_food_day_screen(
        dt.date(2026, 7, 20), [_view(id=1, description="Что-то", protein=10, fat=2, carbs=5)]
    )
    assert "<b>Б10 · Ж2 · У5</b>" in only_macros


def test_macros_line_has_no_bold_and_no_trailing_unit():
    assert formatting._macros_line(30, 12, 60) == "Б30 · Ж12 · У60"


def _day(**kwargs) -> formatting.FoodDayView:
    base = {"date": dt.date(2026, 7, 20), "entries": 1}
    base.update(kwargs)
    return formatting.FoodDayView(**base)


def test_history_list_newest_first():
    days = [
        _day(date=dt.date(2026, 7, 21), entries=1, calories=200.0),
        _day(date=dt.date(2026, 7, 20), entries=2, calories=410.0),
    ]
    text = formatting.build_food_history_list(days)
    assert text.index("21.07.2026") < text.index("20.07.2026")
    assert "2 приёма пищи" in text
    assert "410 ккал" in text


def test_history_list_shows_per_day_macros():
    days = [_day(entries=2, calories=410.0, protein=29, fat=15, carbs=40)]
    text = formatting.build_food_history_list(days)
    assert "410 ккал · Б29 · Ж15 · У40" in text


def test_history_list_skips_macros_line_when_unknown():
    days = [_day(entries=1, calories=200.0)]
    text = formatting.build_food_history_list(days)
    assert "Б " not in text


def test_history_list_omits_totals_line_for_a_day_with_no_numbers():
    """Ни одной цифры за день — не пишем "—", просто дата и число приёмов."""
    days = [_day(date=dt.date(2026, 7, 20), entries=2)]
    text = formatting.build_food_history_list(days)
    assert "20.07.2026 (пн)</b>\n2 приёма пищи" in text
    assert "—" not in text


def test_history_list_folds_the_food_behind_a_collapsible_quote():
    days = [_day(entries=2, calories=410.0, descriptions=["Овсянка", "Кофе"])]
    text = formatting.build_food_history_list(days)
    assert "<blockquote expandable>1. Овсянка\n2. Кофе</blockquote>" in text


def test_history_list_skips_the_quote_without_descriptions():
    days = [_day(entries=1, calories=200.0)]
    text = formatting.build_food_history_list(days)
    assert "<blockquote" not in text


def test_history_list_empty():
    assert "Пока ничего не записано" in formatting.build_food_history_list([])


def test_format_day_month_ru():
    assert formatting.format_day_month_ru(dt.date(2026, 7, 20), today=dt.date(2026, 8, 1)) == "20 июля"


def test_format_day_month_ru_adds_year_only_when_not_current():
    """Год не несёт новой информации в пределах текущего года — на кнопке он
    только отъедает место у самой даты."""
    assert (
        formatting.format_day_month_ru(dt.date(2025, 7, 20), today=dt.date(2026, 8, 1))
        == "20 июля 2025"
    )


def test_food_history_keyboard_matches_day_nav_date_format():
    """История («14 августа») и навигация по дням (food_day_keyboard) должны
    читаться одинаково — раньше история показывала «14.08.2026»."""
    today = dt.date(2026, 8, 15)
    kb = keyboards.food_history_keyboard([dt.date(2026, 8, 14)], page=0, has_next=False, today=today)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "14 августа" in texts
    assert not any("." in t for t in texts)  # никакого дд.мм.гггг


def test_food_history_keyboard_shows_year_for_a_past_year_day():
    today = dt.date(2026, 8, 15)
    kb = keyboards.food_history_keyboard([dt.date(2025, 8, 14)], page=0, has_next=False, today=today)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "14 августа 2025" in texts


# ---------- клавиатуры ----------


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_day_keyboard_has_delete_per_entry_and_day_steps():
    today = dt.date(2026, 7, 21)
    kb = keyboards.food_day_keyboard(dt.date(2026, 7, 20), [7, 8], today=today)
    cbs = _callbacks(kb)
    assert "fd:delask:7" in cbs and "fd:delask:8" in cbs  # спрашивает подтверждение, не удаляет сразу
    assert "fd:day:2026-07-19" in cbs  # шаг назад
    assert "fd:day:2026-07-21" in cbs  # шаг вперёд — день в прошлом
    assert "fd:history:0" in cbs


def test_day_keyboard_history_and_goal_share_a_row():
    kb = keyboards.food_day_keyboard(dt.date(2026, 7, 20), [], today=dt.date(2026, 7, 20))
    rows = kb.inline_keyboard
    history_row = [row for row in rows if row[0].callback_data == "fd:history:0"][0]
    assert [b.callback_data for b in history_row] == ["fd:history:0", "fd:goal"]
    assert rows[-1][0].callback_data == "fd:menu"
    assert rows[-1][0].text == "🏠 Меню"


def test_day_keyboard_hides_step_into_the_future():
    today = dt.date(2026, 7, 20)
    cbs = _callbacks(keyboards.food_day_keyboard(today, [], today=today))
    assert "fd:day:2026-07-21" not in cbs
    assert "fd:day:2026-07-19" in cbs


# ---------- флоу ----------


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(user_id: int, text: str | None = None) -> Message:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    message.text = text
    message.caption = None
    message.chat = SimpleNamespace(id=user_id)
    message.bot = AsyncMock()
    message.message_id = 500
    sent = MagicMock(spec=Message)
    sent.message_id = 501
    sent.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=sent)
    message.reply = AsyncMock()
    return message


def _make_callback(user_id: int, data: str) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.answer = AsyncMock()
    message = MagicMock(spec=Message)
    message.message_id = 501
    message.chat = SimpleNamespace(id=user_id)
    message.text = "экран"
    message.photo = None
    message.bot = AsyncMock()
    message.delete = AsyncMock()
    sent = MagicMock(spec=Message)
    sent.message_id = 502
    message.answer = AsyncMock(return_value=sent)
    callback.message = message
    return callback


async def test_command_opens_today_and_sets_state(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    message = _make_message(user_id)
    state = await _make_state(user_id)

    await food_diary.cmd_food_diary(message, state)

    assert await state.get_state() == FoodDiaryFlow.viewing.state
    assert (await state.get_data())["fd_date"] == "2026-07-20"
    assert "20.07.2026" in message.answer.call_args.args[0]


async def test_text_entry_saves_as_written_without_any_model(user_id, monkeypatch):
    """AI-разбора еды больше нет: текст ложится в открытый день как есть, сразу,
    и ни один AI-модуль не трогается (analyze_food выпилен целиком)."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    assert not hasattr(ai_trainer, "analyze_food")
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-19")

    await food_diary.fd_text_entry(_make_message(user_id, text="гречка с курицей"), state)

    rows = await dbmod.list_food_entries(user_id, "2026-07-19")
    assert [r["description"] for r in rows] == ["гречка с курицей"]
    assert rows[0]["calories"] is None
    assert rows[0]["source"] == "text"
    assert await state.get_state() == FoodDiaryFlow.viewing.state


@pytest.mark.parametrize(
    "caption,description,source",
    [("омлет", "омлет", "photo_text"), (None, "Приём пищи", "photo")],
)
async def test_photo_entry_saves_the_photo_without_analysis(user_id, monkeypatch, caption, description, source):
    """Фото еды больше не разбирается моделью: ложится file_id с подписью (или
    с «Приём пищи», если подписи нет) — файл даже не скачивается."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    message = _make_message(user_id)
    message.caption = caption
    message.photo = [SimpleNamespace(file_id="small", file_size=10), SimpleNamespace(file_id="big", file_size=20)]

    await food_diary.fd_photo_entry(message, state)

    message.bot.download.assert_not_called()
    rows = await dbmod.list_food_entries(user_id, "2026-07-20")
    assert [(r["description"], r["photo_file_id"], r["source"]) for r in rows] == [
        (description, "big", source)
    ]


@pytest.mark.parametrize(
    "text,handled",
    [("овсянка", True), ("/start", False), ("/food_diary", False), ("/help", False)],
)
def test_commands_are_not_swallowed_as_food(text, handled):
    """Роутер дневника стоит раньше workout.router, поэтому его текстовый фильтр
    обязан пропускать команды дальше — иначе /start превратился бы в приём пищи."""
    assert food_diary._NOT_A_COMMAND.resolve(SimpleNamespace(text=text)) is handled


async def test_delete_asks_for_confirmation_before_removing(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    entry_id = await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    callback = _make_callback(user_id, f"fd:delask:{entry_id}")

    await food_diary.fd_delete_ask(callback, state)

    # ничего не удалено — только показан вопрос с кнопками подтверждения
    assert await dbmod.get_food_entry(entry_id) is not None
    shown_text = callback.message.answer.call_args.args[0]
    assert "Овсянка" in shown_text
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"fd:del:{entry_id}" in cbs
    assert "fd:day:2026-07-20" in cbs  # «Отмена» ведёт назад на день записи


async def test_delete_asks_reports_missing_entry(user_id, monkeypatch):
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    callback = _make_callback(user_id, "fd:delask:999999")

    await food_diary.fd_delete_ask(callback, state)

    assert callback.answer.call_args.args[0] == "Такой записи уже нет — открой 🍽 Еда и посмотри актуальный список"


async def test_delete_removes_only_the_owner_entry(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    entry_id = await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)
    other_id = await dbmod.add_food_entry(user_id + 1, "2026-07-20", "Чужая еда")

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)

    await food_diary.fd_delete(_make_callback(user_id, f"fd:del:{entry_id}"), state)
    assert await dbmod.get_food_entry(entry_id) is None

    callback = _make_callback(user_id, f"fd:del:{other_id}")
    await food_diary.fd_delete(callback, state)
    assert await dbmod.get_food_entry(other_id) is not None  # чужая запись цела
    assert callback.answer.call_args.args[0] == "Такой записи уже нет — открой 🍽 Еда и посмотри актуальный список"


async def test_history_lists_logged_days(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 21))
    await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)
    await dbmod.add_food_entry(user_id, "2026-07-21", "Творог", calories=200)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "fd:history:0")

    await food_diary.fd_history(callback, state)

    assert await state.get_state() == FoodDiaryFlow.browsing_history.state
    text = callback.message.answer.call_args.args[0]
    assert "21.07.2026" in text and "20.07.2026" in text


async def test_opening_a_day_from_history_shows_that_day(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 21))
    await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)

    state = await _make_state(user_id)
    callback = _make_callback(user_id, "fd:day:2026-07-20")
    await food_diary.fd_open_day(callback, state)

    assert (await state.get_data())["fd_date"] == "2026-07-20"
    assert await state.get_state() == FoodDiaryFlow.viewing.state
    assert "Овсянка" in callback.message.answer.call_args.args[0]


# ---------- находка 33: КБЖУ, которых не бывает ----------


def test_negative_macros_from_the_model_are_dropped():
    """Минус в ответе модели давал карточку «Итого: -165 ккал · Б-10 · Ж-5 ·
    У-20», такая же строка ложилась в дневник и утягивала итог дня в минус."""
    assert ai_trainer._as_macro(-400, ai_trainer.MAX_FOOD_KCAL) is None
    assert ai_trainer._as_macro("-10 г", ai_trainer.MAX_FOOD_GRAMS) is None


def test_absurdly_large_macros_are_dropped():
    assert ai_trainer._as_macro(999999, ai_trainer.MAX_FOOD_KCAL) is None
    assert ai_trainer._as_macro(50000, ai_trainer.MAX_FOOD_GRAMS) is None


def test_plausible_macros_pass_through_untouched():
    assert ai_trainer._as_macro("420 ккал", ai_trainer.MAX_FOOD_KCAL) == 420.0
    assert ai_trainer._as_macro("~15 г", ai_trainer.MAX_FOOD_GRAMS) == 15.0
    assert ai_trainer._as_macro(0, ai_trainer.MAX_FOOD_KCAL) == 0.0
    assert ai_trainer._as_macro(None, ai_trainer.MAX_FOOD_KCAL) is None


# ---------- «🎯 Цель ккал» ----------


def test_goal_line_shows_remaining_when_under():
    entries = [formatting.FoodEntryView(id=1, description="Обед", calories=1560.0)]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries, kcal_goal=2200)
    assert "Цель 2200 · осталось 640" in text


def test_goal_line_shows_over_when_over():
    entries = [formatting.FoodEntryView(id=1, description="Обед", calories=2350.0)]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries, kcal_goal=2200)
    assert "Цель 2200 · перебор 150" in text


def test_goal_line_absent_without_a_goal():
    entries = [formatting.FoodEntryView(id=1, description="Обед", calories=1560.0)]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries, kcal_goal=None)
    assert "Цель" not in text


def test_goal_line_absent_when_calories_unknown():
    """Данные не подтверждают остаток — строка не показывается вовсе, а не
    врёт нулём (см. tone: утверждение о данных только когда они его подтверждают)."""
    entries = [formatting.FoodEntryView(id=1, description="Обед", calories=None)]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries, kcal_goal=2200)
    assert "Цель" not in text


async def test_kcal_goal_setter_roundtrip(user_id):
    await dbmod.set_kcal_goal(user_id, 2200)
    user = await dbmod.get_user(user_id)
    assert user["kcal_goal"] == 2200
    await dbmod.set_kcal_goal(user_id, None)
    user = await dbmod.get_user(user_id)
    assert user["kcal_goal"] is None


async def test_fd_goal_prompt_asks_for_a_number(user_id):
    state = await _make_state(user_id)
    await state.update_data(fd_date="2026-07-20")
    callback = _make_callback(user_id, "fd:goal")

    await food_diary.fd_goal_prompt(callback, state)

    assert await state.get_state() == FoodDiaryFlow.setting_goal.state
    callback.answer.assert_awaited_once()


async def test_fd_goal_entered_saves_and_returns_to_the_day(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.setting_goal)
    await state.update_data(fd_date="2026-07-20")

    message = _make_message(user_id, "2200")
    await food_diary.fd_goal_entered(message, state)

    user = await dbmod.get_user(user_id)
    assert user["kcal_goal"] == 2200
    message.reply.assert_awaited()
    assert "2200" in message.reply.await_args.args[0]


async def test_fd_goal_entered_rejects_non_numeric(user_id):
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.setting_goal)
    await state.update_data(fd_date="2026-07-20")

    message = _make_message(user_id, "много")
    await food_diary.fd_goal_entered(message, state)

    user = await dbmod.get_user(user_id)
    assert user["kcal_goal"] is None


async def test_fd_goal_entered_rejects_out_of_range(user_id):
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.setting_goal)
    await state.update_data(fd_date="2026-07-20")

    message = _make_message(user_id, "50000")
    await food_diary.fd_goal_entered(message, state)

    user = await dbmod.get_user(user_id)
    assert user["kcal_goal"] is None
