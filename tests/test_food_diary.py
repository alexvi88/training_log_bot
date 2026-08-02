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
    await dbmod.add_food_entry(user_id, "2026-07-20", "Овсянка", calories=350)
    await dbmod.add_food_entry(user_id, "2026-07-20", "Кофе", calories=60)
    await dbmod.add_food_entry(user_id, "2026-07-21", "Творог", calories=200)

    days = await dbmod.list_food_days(user_id)
    assert [r["eaten_on"] for r in days] == ["2026-07-21", "2026-07-20"]
    assert [r["entries"] for r in days] == [1, 2]
    assert [r["calories"] for r in days] == [200, 410]
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
                            '"items": ["овсянка 60 г — 220 ккал", "банан — 90 ккал", "  "], '
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
    assert result["items"] == ["овсянка 60 г — 220 ккал", "банан — 90 ккал"]  # пустые выброшены
    assert result["calories"] == 310.0
    assert result["protein"] == 9.0
    assert result["fat"] is None
    assert result["comment"] == "порция на глаз"
    assert "овсянка с бананом" in captured["messages"][1]["content"]


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


# ---------- тексты экранов ----------


def _view(**kwargs) -> formatting.FoodEntryView:
    base = {"id": 1, "description": "Овсянка"}
    base.update(kwargs)
    return formatting.FoodEntryView(**base)


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
    assert "410 ккал" in text
    assert "2 приёма пищи" in text
    assert "Б 12 · Ж 8 · У 55 г" in text


def test_day_screen_flags_entries_without_calories():
    entries = [_view(id=1, calories=350), _view(id=2, description="Чай")]
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), entries)
    assert "350 ккал" in text
    assert "без калорий: 1" in text


def test_day_screen_escapes_html_in_description():
    text = formatting.build_food_day_screen(dt.date(2026, 7, 20), [_view(description="<b>хак</b>")])
    assert "<b>хак</b>" not in text
    assert "&lt;b&gt;" in text


def test_estimate_text_lists_items_and_macros():
    text = formatting.build_food_estimate_text(
        "Овсянка с бананом",
        ["овсянка 60 г — 220 ккал", "банан — 90 ккал"],
        calories=310, protein=9, fat=6, carbs=62, comment="порция на глаз",
    )
    assert "Овсянка с бананом" in text
    assert "• банан — 90 ккал" in text
    assert "310 ккал" in text
    assert "Б 9 · Ж 6 · У 62 г" in text
    assert "порция на глаз" in text


def test_estimate_text_without_numbers_shows_dash():
    text = formatting.build_food_estimate_text("Что-то съел", [])
    assert "— ккал" in text or "—" in text
    assert "Б " not in text  # строки БЖУ нет вовсе


def test_history_list_newest_first():
    days = [(dt.date(2026, 7, 21), 1, 200.0), (dt.date(2026, 7, 20), 2, 410.0)]
    text = formatting.build_food_history_list(days)
    assert text.index("21.07.2026") < text.index("20.07.2026")
    assert "2 приёма" in text
    assert "410 ккал" in text


def test_history_list_empty():
    assert "Пока ничего не записано" in formatting.build_food_history_list([])


def test_format_day_month_ru():
    assert formatting.format_day_month_ru(dt.date(2026, 7, 20)) == "20 июля"


# ---------- клавиатуры ----------


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_day_keyboard_has_delete_per_entry_and_day_steps():
    today = dt.date(2026, 7, 21)
    kb = keyboards.food_day_keyboard(dt.date(2026, 7, 20), [7, 8], today=today)
    cbs = _callbacks(kb)
    assert "fd:del:7" in cbs and "fd:del:8" in cbs
    assert "fd:day:2026-07-19" in cbs  # шаг назад
    assert "fd:day:2026-07-21" in cbs  # шаг вперёд — день в прошлом
    assert "fd:history:0" in cbs


def test_day_keyboard_hides_step_into_the_future():
    today = dt.date(2026, 7, 20)
    cbs = _callbacks(keyboards.food_day_keyboard(today, [], today=today))
    assert "fd:day:2026-07-21" not in cbs
    assert "fd:day:2026-07-19" in cbs


def test_date_keyboard_offers_neighbours_and_calendar():
    today = dt.date(2026, 7, 20)
    kb = keyboards.food_date_keyboard(
        [today, dt.date(2026, 7, 19), dt.date(2026, 7, 18)], today
    )
    cbs = _callbacks(kb)
    assert cbs[0] == "fd:date:2026-07-20"
    assert "fd:date:2026-07-19" in cbs and "fd:date:2026-07-18" in cbs
    assert "fd:otherdate" in cbs and "fd:cancel" in cbs
    assert "Занести за сегодня" in kb.inline_keyboard[0][0].text


def test_date_keyboard_names_a_past_day_in_words():
    kb = keyboards.food_date_keyboard([dt.date(2026, 7, 18)], dt.date(2026, 7, 20))
    assert kb.inline_keyboard[0][0].text == "✅ Занести за 18 июля"


@pytest.mark.parametrize(
    "viewed,today,expected",
    [
        # сегодняшний день — предлагаем сегодня и два прошлых, а не будущие
        ("2026-07-20", "2026-07-20", ["2026-07-20", "2026-07-19", "2026-07-18"]),
        # открыт прошлый день — соседи по обе стороны
        ("2026-07-10", "2026-07-20", ["2026-07-10", "2026-07-09", "2026-07-11"]),
        # вчера — «завтра» это сегодня, оно ещё допустимо
        ("2026-07-19", "2026-07-20", ["2026-07-19", "2026-07-18", "2026-07-20"]),
    ],
)
def test_date_options(viewed, today, expected):
    got = food_diary._date_options(dt.date.fromisoformat(viewed), dt.date.fromisoformat(today))
    assert [d.isoformat() for d in got] == expected


# ---------- флоу ----------


async def _make_state(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(user_id: int, text: str | None = None) -> Message:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=user_id, username="tester")
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
    callback.from_user = SimpleNamespace(id=user_id, username="tester")
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


async def test_typed_food_goes_to_model_and_shows_confirmation(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction=""):
        return {
            "description": "Овсянка с бананом", "items": ["овсянка 60 г"],
            "calories": 310, "protein": 9, "fat": 6, "carbs": 62, "comment": "",
        }

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", fake_analyze)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20")
    message = _make_message(user_id, text="овсянка с бананом")

    await food_diary.fd_text_entry(message, state)

    assert await state.get_state() == FoodDiaryFlow.confirming.state
    pending = (await state.get_data())["fd_pending"]
    assert pending["description"] == "Овсянка с бананом"
    assert pending["source"] == "text"
    # карточка ушла правкой заглушки «думаю»
    placeholder = message.answer.return_value
    assert "Всё верно?" in placeholder.edit_text.call_args.args[0]


async def test_model_failure_keeps_the_diary_usable(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)

    async def boom(*args, **kwargs):
        raise RuntimeError("модель прилегла")

    monkeypatch.setattr(food_diary.ai_trainer, "analyze_food", boom)

    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date="2026-07-20")
    message = _make_message(user_id, text="овсянка")

    await food_diary.fd_text_entry(message, state)

    assert await state.get_state() == FoodDiaryFlow.viewing.state  # экран дня вернулся
    assert "Не получилось разобрать" in message.answer.return_value.edit_text.call_args.args[0]


async def test_confirm_asks_for_the_date_then_saves(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.confirming)
    await state.update_data(
        fd_date="2026-07-20",
        fd_pending={
            "description": "Овсянка", "items": ["овсянка 60 г"], "calories": 310,
            "protein": 9, "fat": 6, "carbs": 62, "source": "text", "photo_file_id": None,
        },
    )

    await food_diary.fd_confirm(_make_callback(user_id, "fd:ok"), state)
    assert await state.get_state() == FoodDiaryFlow.picking_date.state

    await food_diary.fd_pick_date(_make_callback(user_id, "fd:date:2026-07-18"), state)

    entries = await dbmod.list_food_entries(user_id, "2026-07-18")
    assert [e["description"] for e in entries] == ["Овсянка"]
    assert entries[0]["details"] == "овсянка 60 г"
    assert entries[0]["calories"] == 310
    # ничего не подвисло: экран вернулся на выбранный день, черновик очищен
    assert await state.get_state() == FoodDiaryFlow.viewing.state
    data = await state.get_data()
    assert data["fd_date"] == "2026-07-18" and data["fd_pending"] is None


async def test_typed_date_saves_for_a_past_day(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.picking_date)
    await state.update_data(fd_date="2026-07-20", fd_pending={"description": "Творог"})

    await food_diary.fd_date_typed(_make_message(user_id, text="15.07.2026"), state)

    assert [e["description"] for e in await dbmod.list_food_entries(user_id, "2026-07-15")] == ["Творог"]


async def test_bad_typed_date_does_not_save(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    state = await _make_state(user_id)
    await state.set_state(FoodDiaryFlow.picking_date)
    await state.update_data(fd_date="2026-07-20", fd_pending={"description": "Творог"})
    message = _make_message(user_id, text="когда-то на той неделе")

    await food_diary.fd_date_typed(message, state)

    assert await dbmod.count_food_days(user_id) == 0
    assert (await state.get_data())["fd_pending"] is not None  # черновик цел, можно повторить
    message.reply.assert_awaited()


async def test_correction_reruns_the_model_with_the_previous_guess(user_id, monkeypatch):
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    seen = {}

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction=""):
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


async def test_typed_correction_without_pressing_the_button(user_id, monkeypatch):
    """Ответить карточке текстом — та же правка, что и по кнопке «✏️ Поправить»."""
    monkeypatch.setattr(food_diary.timeutil, "user_today", lambda user: dt.date(2026, 7, 20))
    monkeypatch.setattr(food_diary.ai_trainer, "is_configured", lambda: True)
    seen = {}

    async def fake_analyze(uid, text="", image_data_url=None, previous=None, correction=""):
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
    assert callback.answer.call_args.args[0] == "Запись не найдена"


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
