"""Settings screen: unit/formula toggles and the pushes on/off switch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from handlers import settings

pytestmark = pytest.mark.asyncio


def _make_callback(user_id: int, data: str):
    message = MagicMock()
    message.delete = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
    callback.message = message
    callback.data = data
    callback.answer = AsyncMock()
    return callback


async def _make_state(user_id: int) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


async def test_settings_pushes_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 0

    callback = _make_callback(user_id, "settings:pushes")
    await settings.settings_pushes(callback, state)
    assert (await db.get_user(user_id))["pushes_enabled"] == 1


async def test_show_settings_reflects_pushes_state_in_keyboard_labels(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_on = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any(label == "🔔 Пуши: шлю" for label in labels_on)

    await db.update_user(user_id, pushes_enabled=0)
    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    sent_text = callback.message.answer.call_args.kwargs["reply_markup"]
    labels_off = [b.text for row in sent_text.inline_keyboard for b in row]
    assert any(label == "🔕 Пуши: молчу" for label in labels_off)


async def test_settings_food_macros_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)

    assert (await db.get_user(user_id))["food_macros_enabled"] == 1  # по умолчанию считаем

    callback = _make_callback(user_id, "settings:food_macros")
    await settings.settings_food_macros(callback, state)
    assert (await db.get_user(user_id))["food_macros_enabled"] == 0

    callback = _make_callback(user_id, "settings:food_macros")
    await settings.settings_food_macros(callback, state)
    assert (await db.get_user(user_id))["food_macros_enabled"] == 1


async def test_settings_progression_toggles_off_then_on(fresh_db, user_id):
    db = fresh_db
    state = await _make_state(user_id)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 1  # on by default

    callback = _make_callback(user_id, "settings:progression")
    await settings.settings_progression(callback, state)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 0

    callback = _make_callback(user_id, "settings:progression")
    await settings.settings_progression(callback, state)
    assert (await db.get_user(user_id))["progression_hint_enabled"] == 1


async def test_formula_switch_asks_before_rewriting_every_e1rm(fresh_db, user_id):
    """Switching the formula recomputes every e1RM, record and chart in the
    history — bigger than the unit switch, which already asked."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formula")

    await settings.settings_formula_confirm(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "epley"  # unchanged — it only asked
    sent = callback.message.answer.await_args
    text = sent.args[0] if sent.args else sent.kwargs["text"]
    # Русское название формулы, а не сырой код "brzycki" — та же логика, что и
    # у тоста после подтверждения (test_confirming_the_formula_switch_is_a_toast_not_a_modal).
    assert "Бжицки" in text
    assert "Пересчитаю" in text


async def test_confirming_the_formula_switch_applies_it(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formulayes")

    await settings.settings_formula(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "brzycki"


async def test_confirming_the_formula_switch_is_a_toast_not_a_modal(fresh_db, user_id):
    """«Формула e1RM: brzycki» за модалкой с ОК — лишний тап на итог, который и
    так виден на экране настроек; после подтверждающего yes/no это уже не
    предупреждение, а короткая реплика тренера («Перевёл на Бжицки»)."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formulayes")

    await settings.settings_formula(callback, state)

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.call_args
    assert "Бжицки" in args[0]
    assert kwargs.get("show_alert") is False


async def test_cancelling_the_formula_switch_changes_nothing(fresh_db, user_id):
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:formulano")

    await settings.settings_formula_cancel(callback, state)

    user = await fresh_db.get_user(user_id)
    assert user["e1rm_formula"] == "epley"


# ---------- «🧬 Обо мне»: что AI-тренер записал с твоих слов ----------


async def test_profile_screen_shows_what_the_trainer_wrote(fresh_db, user_id):
    """Поля профиля пишет тренер, не дожидаясь просьбы (см.
    ai_trainer.save_athlete_profile) — до этого экрана их нельзя было ни
    увидеть, ни поправить, при том что от них зависит подбор программ."""
    import json

    import ai_trainer
    import ui

    payload = json.loads(await ai_trainer.execute_tool(
        user_id, "save_athlete_profile",
        {"goal": "масса", "experience": "средний", "equipment": ["штанга", "гантели"]},
    ))
    assert payload["saved"] is True

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(_make_callback(user_id, "settings:profile"), await _make_state(user_id))
    finally:
        ui.safe_edit = original

    assert "масса" in seen["text"]
    assert "средний" in seen["text"]
    # Оборудование лежит JSON-строкой, а показываться должно по-человечески.
    assert "штанга, гантели" in seen["text"]
    assert '["' not in seen["text"]


async def test_profile_screen_marks_what_is_still_unknown(fresh_db, user_id):
    """Прочерк, а не пропущенная строка: половина ценности экрана в том, чтобы
    видеть, чего тренер про тебя ещё не знает."""
    import ui

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(_make_callback(user_id, "settings:profile"), await _make_state(user_id))
    finally:
        ui.safe_edit = original

    # Считаем прочерки-значения, а не все тире в тексте — во вводной фразе
    # экрана своё.
    empty = [line for line in seen["text"].split("\n") if line.endswith("</b> —")]
    # Четыре, а не пять: дни в неделю тренер больше не запоминает.
    assert len(empty) == 4
    assert "Ограничения" in seen["text"]
    # Пустое состояние зовёт, а не констатирует (см. TONE_OF_VOICE.md).
    assert "AI-тренер" in seen["text"]


async def test_profile_can_be_cleared(fresh_db, user_id):
    import ai_trainer
    import ui

    await ai_trainer.execute_tool(user_id, "save_athlete_profile", {"limitations": "болит плечо"})

    async def fake_edit(callback, text, **kwargs):
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile_clear(
            _make_callback(user_id, "settings:profileclear"), await _make_state(user_id)
        )
    finally:
        ui.safe_edit = original

    assert (await fresh_db.get_user(user_id))["limitations"] is None


async def test_profile_button_is_on_the_settings_screen():
    import keyboards

    kb = keyboards.settings_keyboard("kg", "epley", True, True, True)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert "settings:profile" in callbacks


async def test_settings_keyboard_groups_into_three_blocks():
    """Три визуальных блока — «Профиль», «Как разговариваю», «Данные» — и
    порядок кнопок следует ровно этому порядку (см. заголовки в тексте экрана,
    handlers.settings.show_settings)."""
    import keyboards

    kb = keyboards.settings_keyboard(
        "kg", "epley", True, True, True, show_mcp=True, show_feedback=True
    )
    rows = [[b.callback_data for b in row] for row in kb.inline_keyboard]

    # Профиль: единицы+пояс, язык+формула — парами по 2, короткие подписи.
    assert rows[0] == ["settings:unit", "settings:tz"]
    assert rows[1] == ["settings:lang", "settings:formula"]
    # Как разговариваю: пять тумблеров, по одному в ряд — длинные подписи.
    assert rows[2] == ["settings:progression"]
    assert rows[3] == ["settings:pushes"]
    assert rows[4] == ["settings:ai_comments"]
    assert rows[5] == ["settings:food_macros"]
    assert rows[6] == ["settings:card_detail"]
    # Данные: что тренер знает, экспорт, импорт, MCP, отзыв — тоже по одному.
    assert rows[7] == ["settings:profile"]
    assert rows[8] == ["settings:export"]
    assert rows[9] == ["settings:import"]
    assert rows[10] == ["settings:mcp"]
    assert rows[11] == ["feedback:open"]
    assert rows[12] == ["invite:show"]
    assert rows[13] == ["settings:back"]


async def test_settings_keyboard_hides_feedback_button_without_admin():
    import keyboards

    kb_with = keyboards.settings_keyboard("kg", "epley", True, True, True, show_feedback=True)
    kb_without = keyboards.settings_keyboard("kg", "epley", True, True, True, show_feedback=False)

    callbacks_with = [b.callback_data for row in kb_with.inline_keyboard for b in row]
    callbacks_without = [b.callback_data for row in kb_without.inline_keyboard for b in row]

    assert "feedback:open" in callbacks_with
    assert "feedback:open" not in callbacks_without


async def test_show_settings_passes_feedback_availability(fresh_db, user_id, monkeypatch):
    """show_settings скрывает «💬 Отзыв» ровно тогда же, когда его прячет ошибка
    последнего рубежа (config.feedback_available), — один и тот же выключатель."""
    import config

    callback = _make_callback(user_id, "menu:settings")
    state = await _make_state(user_id)

    monkeypatch.setattr(config, "ADMIN_ID", 123456)
    await settings.show_settings(callback, state)
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    assert "feedback:open" in [b.callback_data for row in kb.inline_keyboard for b in row]

    monkeypatch.setattr(config, "ADMIN_ID", None)
    await settings.show_settings(callback, state)
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    assert "feedback:open" not in [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_show_settings_screen_has_three_section_headers(fresh_db, user_id):
    """Текст экрана несёт заголовки трёх блоков — жирными строками, в том же
    порядке, в каком идут блоки кнопок."""
    callback = _make_callback(user_id, "menu:settings")
    state = await _make_state(user_id)

    await settings.show_settings(callback, state)

    text = callback.message.answer.call_args.args[0]
    profile_pos = text.index("Профиль")
    voice_pos = text.index("Как разговариваю")
    data_pos = text.index("Данные")
    assert profile_pos < voice_pos < data_pos


async def test_toggle_labels_share_one_first_person_verb_construction():
    """Раньше у четырёх тумблеров были разные формы («вкл»/«выкл»,
    «включены»/«выключены», «считаю»/«не считаю», «подробно»/«компактно») —
    теперь у всех глагол от первого лица, а не канцелярское «вкл»/причастие."""
    import keyboards

    on_kb = keyboards.settings_keyboard(
        "kg", "epley", True, True, True, food_macros_enabled=True, show_extra_stats=True
    )
    off_kb = keyboards.settings_keyboard(
        "kg", "epley", False, False, False, food_macros_enabled=False, show_extra_stats=False
    )
    on_texts = [b.text for row in on_kb.inline_keyboard for b in row]
    off_texts = [b.text for row in off_kb.inline_keyboard for b in row]

    assert "🎯 Подсказки прогрессии: подсказываю" in on_texts
    assert "🎯 Подсказки прогрессии: молчу" in off_texts
    assert "🔔 Пуши: шлю" in on_texts
    assert "🔕 Пуши: молчу" in off_texts
    assert "🤖 Комментарии тренера: комментирую" in on_texts
    assert "🤖 Комментарии тренера: молчу" in off_texts
    assert "🔢 КБЖУ: считаю" in on_texts
    assert "📝 КБЖУ: не считаю" in off_texts
    # Раньше «Карточка тренировки: подробно/компактно» не говорило, что именно
    # переключается — это строка e1RM на итоговой карточке.
    assert "📊 e1RM на карточке: показываю" in on_texts
    assert "📊 e1RM на карточке: прячу" in off_texts
    for text in on_texts + off_texts:
        assert len(text) <= 40, f"кнопка слишком длинная для Telegram: {text!r}"


# ---------- экран настроек говорит по-русски, а не телеметрией ----------


async def test_settings_keyboard_shows_russian_unit_and_formula_names():
    """«Единицы: kg» и «Формула e1RM: brzycki» — телеметрия, а не реплики
    тренера (TONE_OF_VOICE.md). Внутренние значения ("kg"/"lb",
    "epley"/"brzycki") не меняются — русифицируется только то, что видит
    человек."""
    import keyboards

    kb = keyboards.settings_keyboard("lb", "brzycki", True, True, True)
    labels = [b.text for row in kb.inline_keyboard for b in row]

    assert any("фунты" in label for label in labels)
    assert any("Бжицки" in label for label in labels)
    assert not any("lb" in label for label in labels)
    assert not any("brzycki" in label for label in labels)

    kb_kg = keyboards.settings_keyboard("kg", "epley", True, True, True)
    labels_kg = [b.text for row in kb_kg.inline_keyboard for b in row]
    assert any("кг" in label for label in labels_kg)
    assert any("Эпли" in label for label in labels_kg)


async def test_settings_header_is_a_coach_line_not_a_label(fresh_db, user_id):
    """Заголовок экрана — короткая реплика тренера, а не «🔧 Настройки:»."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "menu:settings")

    await settings.show_settings(callback, state)

    text = callback.message.answer.call_args.args[0]
    assert text != "🔧 Настройки:"
    assert not text.rstrip().endswith(":")


async def test_profile_screen_speaks_in_the_first_person(fresh_db, user_id):
    """Один персонаж на весь продукт (TONE_OF_VOICE.md): бот говорит «записал»,
    а не «тренер записал» — про самого себя в третьем лице он не говорит."""
    import json

    import ai_trainer
    import ui

    payload = json.loads(
        await ai_trainer.execute_tool(user_id, "save_athlete_profile", {"goal": "масса"})
    )
    await fresh_db.update_user(user_id, **payload["fields"])

    seen = {}

    async def fake_edit(callback, text, **kwargs):
        seen["text"] = text
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(
            _make_callback(user_id, "settings:profile"), await _make_state(user_id)
        )
    finally:
        ui.safe_edit = original

    assert "Записал с твоих слов" in seen["text"]
    assert "напиши сюда" in seen["text"]


async def test_profile_screen_listens_to_what_you_type(fresh_db, user_id):
    """Экран зовёт написать, как правильно, — значит, у этого зова обязан быть
    слушатель.

    На проде было ровно наоборот: человек стоял на «ЧТО Я ПРО ТЕБЯ ЗНАЮ», писал
    «убери дней в неделю и ограничения» и получал «Не понял 🤔 Вопрос тренеру —
    жми AI-тренер». Экран врал прямым текстом."""
    import ui
    from fsm import AITrainerFlow, SettingsFlow

    await fresh_db.update_user(user_id, goal="масса")
    state = await _make_state(user_id)

    async def fake_edit(callback, text, **kwargs):
        return MagicMock()

    original, ui.safe_edit = ui.safe_edit, fake_edit
    try:
        await settings.settings_profile(_make_callback(user_id, "settings:profile"), state)
    finally:
        ui.safe_edit = original

    assert await state.get_state() == SettingsFlow.profile.state

    seen = {}

    async def fake_handle(message, st, question, history_question, **kwargs):
        seen["question"] = question
        seen["history"] = history_question

    from handlers import ai_trainer as ai_handler

    original_handle, ai_handler._handle_question = ai_handler._handle_question, fake_handle
    try:
        message = MagicMock()
        message.text = "убери ограничения"
        message.from_user = SimpleNamespace(id=user_id, username="tester", language_code=None)
        message.reply = AsyncMock()
        await settings.profile_correction(message, state)
    finally:
        ai_handler._handle_question = original_handle

    # Тренеру уходит и сама фраза, и рамка про то, что это правка памяти, —
    # иначе «убери ограничения» читается как правка программы.
    assert "убери ограничения" in seen["question"]
    assert "forget" in seen["question"]
    assert seen["history"] == "убери ограничения"
    # Дальше человек уже в разговоре: следующая реплика не должна упереться
    # в «Не понял».
    assert await state.get_state() == AITrainerFlow.chatting.state
