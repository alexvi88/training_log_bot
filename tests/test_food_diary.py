"""Дневник питания: слой БД, разбор ответа модели, тексты экранов и сам флоу."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

import ai_trainer
import config
import db as dbmod
import formatting
import i18n
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


async def test_analyze_food_normalizes_model_response(monkeypatch, user_id):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"description": "Овсянка с бананом", '
                            '"items": ['
                            '{"name": "овсянка", "portion": "60 г", "calories": "220 ккал", '
                            '"protein": 6, "fat": 4, "carbs": 36}, '
                            '{"name": "банан", "portion": "1 шт", "calories": 90}, '
                            '{"name": "  "}], '
                            '"calories": "310 ккал", "protein": 9, "fat": null, "carbs": 62, '
                            '"comment": "порция на глаз"}'
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )

    result = await ai_trainer.analyze_food(user_id, text="овсянка с бананом")

    assert result["description"] == "Овсянка с бананом"
    assert [i["name"] for i in result["items"]] == ["овсянка", "банан"]  # безымянный выброшен
    assert result["items"][0]["portion"] == "60 г"
    assert result["items"][0]["protein"] == 6.0
    # ккал пункта пересчитаны из его же БЖУ (6·4 + 4·9 + 36·4), а не взяты из ответа
    assert result["items"][0]["calories"] == 204.0
    assert result["items"][1]["fat"] is None  # чего не было в ответе — то None
    assert result["items"][1]["calories"] == 90.0  # без полного БЖУ ккал модели не трогаем
    # итог — сумма раскладки, а не отдельная догадка модели (в ответе было 310)
    assert result["calories"] == 294.0
    assert result["protein"] == 6.0
    assert result["comment"] == "порция на глаз"
    assert "овсянка с бананом" in captured["messages"][1]["content"]


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


async def test_totals_reconcile_with_the_breakdown(monkeypatch, user_id):
    """Главная проверка: показанные БЖУ, умноженные на 4/9/4, дают показанный
    итог, и он же равен сумме калорий по строкам."""

    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"description": "Чизбургер", "items": ['
                            '{"name": "Булочка", "portion": "80 г", "calories": 220,'
                            ' "protein": 7.4, "fat": 4.2, "carbs": 38.6},'
                            '{"name": "Котлета", "portion": "100 г", "calories": 250,'
                            ' "protein": 17.7, "fat": 19.4, "carbs": 0.3},'
                            '{"name": "Сыр", "portion": "25 г", "calories": 100,'
                            ' "protein": 6.1, "fat": 8.3, "carbs": 0.6}],'
                            '"calories": 620, "protein": 28, "fat": 32, "carbs": 48}'
                        )
                    )
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )

    result = await ai_trainer.analyze_food(user_id, text="чизбургер")

    by_formula = result["protein"] * 4 + result["fat"] * 9 + result["carbs"] * 4
    assert result["calories"] == by_formula
    assert result["calories"] == sum(i["calories"] for i in result["items"])
    # и каждая строка сходится сама с собой
    for item in result["items"]:
        assert item["calories"] == item["protein"] * 4 + item["fat"] * 9 + item["carbs"] * 4


async def test_analyze_food_uses_its_own_cache_slot(monkeypatch, user_id):
    """FOOD_ANALYSIS_SYSTEM_PROMPT не похож на шапку основного чата — под общим
    conv-id вытеснял бы её слот на каждый разбор еды."""
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"description": "еда"}'))],
            usage=None,
        )

    monkeypatch.setattr(
        ai_trainer, "_get_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))),
    )

    await ai_trainer.analyze_food(user_id, text="овсянка")

    assert captured["extra_headers"]["x-grok-conv-id"] == f"food-{user_id}"


async def test_analyze_food_sends_image_and_correction(monkeypatch, user_id):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"description": "Груша"}'))],
            usage=None,
        )

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )

    await ai_trainer.analyze_food(
        user_id,
        text="",
        image_data_url="data:image/jpeg;base64,AAA",
        previous={"description": "Банан"},
        correction="это груша",
    )

    content = captured["messages"][1]["content"]
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,AAA"
    assert "Банан" in content[0]["text"]  # прошлая догадка уходит модели
    assert "это груша" in content[0]["text"]


async def test_describe_only_uses_the_no_macros_prompt(monkeypatch, user_id):
    """with_macros=False должен переключать промпт и не считать КБЖУ."""
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"is_food": true, "description": "Протеин Whey — 30 г", "comment": ""}'
            ))],
            usage=None,
        )

    monkeypatch.setattr(
        ai_trainer, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    )

    result = await ai_trainer.analyze_food(
        user_id,
        previous={"description": "Протеин Whey"},
        correction="30г",
        with_macros=False,
    )

    assert captured["messages"][0]["content"] == ai_trainer._with_language_tail(
        ai_trainer.FOOD_DESCRIBE_SYSTEM_PROMPT
    )
    assert result == {
        "is_food": True, "description": "Протеин Whey — 30 г", "items": [],
        "calories": None, "protein": None, "fat": None, "carbs": None, "comment": "",
    }


def test_describe_only_prompt_tells_the_model_to_fold_portion_into_description():
    """Регрессия на баг: без этой инструкции модель кладёт уточнение вроде
    «30 г» в comment, а comment нигде не сохраняется — правка теряется целиком
    (см. отчёт пользователя «нигде не сохранилось что 30г»)."""
    assert "ПРЯМО В description" in ai_trainer.FOOD_DESCRIBE_SYSTEM_PROMPT
    assert "единственное поле, которое реально сохраняется" in ai_trainer.FOOD_DESCRIBE_SYSTEM_PROMPT


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


def test_estimate_text_lists_items_with_their_own_macros():
    text = formatting.build_food_estimate_text(
        "Овсянка с бананом",
        [
            _item(name="Овсянка", portion="60 г", calories=220, protein=6, fat=4, carbs=36),
            _item(name="Банан", portion="1 шт", calories=90),
        ],
        calories=310, protein=9, fat=6, carbs=62, comment="порция на глаз",
    )
    assert "Овсянка с бананом" in text
    assert "• Овсянка — 60 г — 220 ккал (Б6 · Ж4 · У36)" in text
    assert "• Банан — 1 шт — 90 ккал" in text  # без макросов — без скобок
    assert "(Б" not in text.split("Банан")[1].split("\n")[0]
    assert "Итого: <b>310 ккал · Б9 · Ж6 · У62</b>" in text
    assert "порция на глаз" in text
    assert "Всё верно?" not in text  # кнопки под карточкой уже спрашивают это


def test_estimate_text_skips_breakdown_for_a_single_item():
    """Один компонент дублирует название приёма — раскладку не показываем
    (та же логика, что в build_food_day_screen)."""
    text = formatting.build_food_estimate_text(
        "Сникерс",
        [_item(name="Сникерс", portion="1 батончик 50 г", calories=256, protein=4, fat=12, carbs=33)],
        calories=256, protein=4, fat=12, carbs=33,
    )
    assert "• Сникерс" not in text
    assert "Итого: <b>256 ккал · Б4 · Ж12 · У33</b>" in text


def test_macros_line_has_no_bold_and_no_trailing_unit():
    assert formatting._macros_line(30, 12, 60) == "Б30 · Ж12 · У60"


def test_estimate_text_without_numbers_skips_totals_entirely():
    """Без оценки (режим "без КБЖУ", или модель ничего не разобрала) карточка
    не выдумывает плейсхолдер вида "Итого: —" — просто ничего не показывает."""
    text = formatting.build_food_estimate_text("Что-то съел", [])
    assert "Итого" not in text
    assert "Б " not in text


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


async def test_text_entry_saves_immediately_when_macros_are_off(user_id, monkeypatch):
    """С выключенным КБЖУ текстовый ввод не зовёт модель и не показывает
    карточку — сохраняется как есть, сразу в текущий день."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    await dbmod.update_user(user_id, food_macros_enabled=0)
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fail_if_called)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20")
    message = _make_message(user_id, text="овсянка с бананом")

    await food_diary.fd_text_entry(message, state)

    assert not called
    entries = await dbmod.list_food_entries(user_id, "2026-07-20")
    assert [e["description"] for e in entries] == ["овсянка с бананом"]
    assert entries[0]["calories"] is None
    assert await state.get_state() == FoodDiaryFlow.viewing.state


async def test_typed_food_goes_to_model_and_shows_confirmation(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction="", with_macros=True):
        return {
            "description": "Овсянка с бананом",
            "items": [{"name": "овсянка", "portion": "60 г", "calories": 220,
                       "protein": 6, "fat": 4, "carbs": 36}],
            "calories": 310, "protein": 9, "fat": 6, "carbs": 62, "comment": "",
        }

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20", fd_screen_id=999)
    message = _make_message(user_id, text="овсянка с бананом")

    await food_diary.fd_text_entry(message, state)

    assert await state.get_state() == FoodDiaryFlow.confirming.state
    pending = (await state.get_data())["fd_pending"]
    assert pending["description"] == "Овсянка с бананом"
    assert pending["source"] == "text"
    # карточка ушла правкой заглушки «думаю»
    placeholder = message.answer.return_value
    assert "Овсянка с бананом" in placeholder.edit_text.call_args.args[0]
    # экран дня («напиши, что съел») остался — не удалялся ради заглушки
    message.bot.delete_message.assert_not_called()
    # вопрос "Всё верно?" отдельной строкой убран — кнопки под карточкой сами
    # спрашивают то же самое
    assert "Всё верно?" not in placeholder.edit_text.call_args.args[0]


async def test_typed_food_reacts_to_the_message_immediately(user_id, monkeypatch):
    """👀 lands on the user's own message right away — an instant "заметил",
    independent of the "🤔 Разбираю…" placeholder that still turns into the
    card by editing (see _analyze_and_show)."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction="", with_macros=True):
        return {
            "description": "Овсянка", "items": [], "calories": 300,
            "protein": 9, "fat": 6, "carbs": 62, "comment": "",
        }

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20", fd_screen_id=999)
    message = _make_message(user_id, text="овсянка")

    await food_diary.fd_text_entry(message, state)

    message.bot.set_message_reaction.assert_awaited_once()
    kwargs = message.bot.set_message_reaction.await_args.kwargs
    assert kwargs["chat_id"] == message.chat.id
    assert kwargs["message_id"] == message.message_id
    assert kwargs["reaction"][0].emoji == "👀"


async def test_reaction_failure_does_not_break_food_analysis(user_id, monkeypatch):
    """Reactions can be unavailable in some chats — a TelegramBadRequest from
    set_message_reaction must not stop the analysis or the confirmation card."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction="", with_macros=True):
        return {
            "description": "Овсянка", "items": [], "calories": 300,
            "protein": 9, "fat": 6, "carbs": 62, "comment": "",
        }

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20", fd_screen_id=999)
    message = _make_message(user_id, text="овсянка")
    message.bot.set_message_reaction = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="reactions not available")
    )

    await food_diary.fd_text_entry(message, state)

    assert await state.get_state() == FoodDiaryFlow.confirming.state
    placeholder = message.answer.return_value
    assert "Овсянка" in placeholder.edit_text.call_args.args[0]


async def test_model_failure_keeps_the_diary_usable(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def boom(*args, **kwargs):
        raise RuntimeError("модель прилегла")

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", boom)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20", fd_screen_id=999)
    message = _make_message(user_id, text="овсянка")

    await food_diary.fd_text_entry(message, state)

    assert await state.get_state() == FoodDiaryFlow.viewing.state
    assert "Не получилось разобрать" in message.answer.return_value.edit_text.call_args.args[0]
    # экран дня цел — ни разу не удалялся и не перерисовывался
    message.bot.delete_message.assert_not_called()
    assert (await state.get_data())["fd_screen_id"] == 999


async def test_confirm_saves_straight_into_the_viewed_day(user_id, monkeypatch):
    """Нет отдельного шага «за какую дату» — запись уходит в тот день, что
    открыт на экране, сразу по «✅ Всё верно»."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.confirming)
    await state.update_data(
        fd_date="2026-07-18",  # переключились стрелками на прошлый день до ввода еды
        fd_pending={
            "description": "Овсянка",
            "items": [{"name": "овсянка", "portion": "60 г", "calories": 220,
                       "protein": None, "fat": None, "carbs": None}],
            "calories": 310,
            "protein": 9, "fat": 6, "carbs": 62, "source": "text", "photo_file_id": None,
        },
    )

    await food_diary.fd_confirm(_make_callback(user_id, "fd:ok"), state)

    entries = await dbmod.list_food_entries(user_id, "2026-07-18")
    assert [e["description"] for e in entries] == ["Овсянка"]
    assert entries[0]["calories"] == 310
    stored_items = food_diary._parse_items(entries[0]["details"])
    assert [i.name for i in stored_items] == ["овсянка"]
    assert stored_items[0].portion == "60 г"
    assert stored_items[0].calories == 220
    # ничего не подвисло: экран того же дня, черновик очищен
    assert await state.get_state() == FoodDiaryFlow.viewing.state
    data = await state.get_data()
    assert data["fd_date"] == "2026-07-18" and data["fd_pending"] is None


async def test_correction_reruns_the_model_with_the_previous_guess(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    seen = {}

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction="", with_macros=True):
        seen["previous"] = previous
        seen["correction"] = correction
        return {"description": "Груша", "items": [], "calories": 80, "comment": ""}

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.correcting)
    await state.update_data(
        fd_date="2026-07-20",
        fd_pending={"description": "Банан", "items": [], "calories": 90,
                    "photo_file_id": "AgAC", "source": "photo"},
    )

    await food_diary.fd_correction(_make_message(user_id, text="это груша"), state)

    assert seen["correction"] == "это груша"
    assert seen["previous"]["description"] == "Банан"
    assert "photo_file_id" not in seen["previous"]  # служебные поля модели не нужны
    pending = (await state.get_data())["fd_pending"]
    assert pending["description"] == "Груша"
    assert pending["photo_file_id"] == "AgAC"  # фото от исходного сообщения не потерялось
    assert await state.get_state() == FoodDiaryFlow.confirming.state


async def test_fix_button_keeps_the_estimate_visible(user_id, monkeypatch):
    """Нажатие «✏️ Поправить» не должно стирать карточку с догадкой модели —
    иначе непонятно, что именно поправлять."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.confirming)
    await state.update_data(
        fd_date="2026-07-20",
        fd_pending={
            "description": "Гранола с протеином",
            "items": [
                {"name": "Протеин", "portion": "30 г", "calories": 120,
                 "protein": 24, "fat": 1, "carbs": 3},
                {"name": "Гранола", "portion": "150 г", "calories": 630,
                 "protein": 15, "fat": 23, "carbs": 98},
            ],
            "calories": 750, "protein": 39, "fat": 24, "carbs": 101, "comment": "",
        },
    )

    callback = _make_callback(user_id, "fd:fix")
    await food_diary.fd_fix(callback, state)

    assert await state.get_state() == FoodDiaryFlow.correcting.state
    shown_text = callback.message.answer.call_args.args[0]
    assert "Гранола с протеином" in shown_text
    assert "Протеин — 30 г — 120 ккал" in shown_text
    assert "750 ккал" in shown_text
    assert i18n.t("food.correct_hint") in shown_text
    # HTML-разметку карточки обязаны отрисовать, а не показать тегами как есть
    assert callback.message.answer.call_args.kwargs["parse_mode"] == "HTML"


async def test_typed_correction_without_pressing_the_button(user_id, monkeypatch):
    """Ответить карточке текстом — та же правка, что и по кнопке «✏️ Поправить»."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    seen = {}

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction="", with_macros=True):
        seen["correction"] = correction
        return {"description": "Груша", "items": [], "calories": 80, "comment": ""}

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.confirming)
    await state.update_data(fd_date="2026-07-20", fd_pending={"description": "Банан"})

    await food_diary.fd_correction(_make_message(user_id, text="это груша"), state)

    assert seen["correction"] == "это груша"
    assert (await state.get_data())["fd_pending"]["description"] == "Груша"


@pytest.mark.parametrize(
    "text,handled",
    [("овсянка", True), ("/start", False), ("/food_diary", False), ("/help", False)],
)
def test_commands_are_not_swallowed_as_food(text, handled):
    """Роутер дневника стоит раньше workout.router, поэтому его текстовый фильтр
    обязан пропускать команды дальше — иначе /start превратился бы в приём пищи."""
    assert food_diary._NOT_A_COMMAND.resolve(SimpleNamespace(text=text)) is handled


async def test_cancel_drops_the_draft(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.confirming)
    await state.update_data(fd_date="2026-07-20", fd_pending={"description": "Овсянка"})

    await food_diary.fd_cancel(_make_callback(user_id, "fd:cancel"), state)

    assert (await state.get_data())["fd_pending"] is None
    assert await dbmod.count_food_days(user_id) == 0
    assert await state.get_state() == FoodDiaryFlow.viewing.state


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


async def test_unconfigured_model_says_so_instead_of_failing(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: False)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    message = _make_message(user_id, text="овсянка")

    await food_diary.fd_text_entry(message, state)

    message.reply.assert_awaited()
    assert "не настроено" in message.reply.call_args.args[0]
    assert await dbmod.count_food_days(user_id) == 0


async def test_analysis_spends_the_daily_quota(user_id, monkeypatch):
    """Разбор еды — платный вызов, и до этого он был единственным без счётчика."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(
        food_diary.ai_trainer, "analyze_food",
        AsyncMock(return_value={"is_food": True, "description": "овсянка", "calories": 350}),
    )
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)

    await food_diary.fd_text_entry(_make_message(user_id, text="овсянка"), state)

    assert await dbmod.get_ai_food_count_today(user_id) == 1


async def test_spent_quota_still_saves_the_text_without_the_model(user_id, monkeypatch):
    """Кончилась квота — дневник не запирается: запись сохраняется как есть,
    просто без калорий. Пейволл на собственную еду мы не ставим."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    analyze = AsyncMock()
    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", analyze)
    await dbmod.increment_ai_food_count(user_id)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20")
    message = _make_message(user_id, text="гречка с курицей")

    await food_diary.fd_text_entry(message, state)

    analyze.assert_not_awaited()
    entries = await dbmod.list_food_entries(user_id, "2026-07-20")
    assert [e["description"] for e in entries] == ["гречка с курицей"]
    assert entries[0]["calories"] is None
    assert "завтра" in message.reply.await_args.args[0]


async def test_spent_quota_stops_the_photo_before_the_model(user_id, monkeypatch):
    """У фото такого запасного пути нет — просим написать словами."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(config, "AI_FOOD_DAILY_LIMIT", 1)
    analyze = AsyncMock()
    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", analyze)
    await dbmod.increment_ai_food_count(user_id)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    message = _make_message(user_id)

    await food_diary._analyze_and_show(message, state, image_data_url="data:image/jpeg;base64,x")

    analyze.assert_not_awaited()
    assert "словами" in message.reply.await_args.args[0]
    assert await dbmod.count_food_days(user_id) == 0


async def test_long_meal_folds_extra_items_instead_of_dropping_them(monkeypatch, user_id):
    """Регрессия: список резался на шестой позиции, а итог пересчитывался по
    оставшимся — обед из десяти блюд молча худел на пару сотен килокалорий, и
    при этом в названии приёма пропавшие блюда упоминались."""
    items = ", ".join(
        f'{{"name": "блюдо {i}", "portion": "100 г", "calories": 100, '
        f'"protein": 10, "fat": 0, "carbs": 15}}'
        for i in range(1, 11)
    )

    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f'{{"description": "Обед", "items": [{items}]}}'
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    monkeypatch.setattr(
        ai_trainer,
        "_get_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))),
    )

    result = await ai_trainer.analyze_food(user_id, text="очень плотный обед")

    # Шесть строк по одному блюду плюс одна свёрнутая — а не шесть и молчание.
    assert len(result["items"]) == 7
    assert result["items"][-1]["name"] == "и ещё 4 позиции"
    assert result["items"][-1]["calories"] == 400
    # Итог по-прежнему равен сумме строк, но теперь строки покрывают весь обед.
    assert result["calories"] == 1000
    assert result["protein"] == 100


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
