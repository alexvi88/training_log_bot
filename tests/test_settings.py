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


async def test_formula_buttons_from_old_messages_just_redraw_settings(fresh_db, user_id):
    """Выбор формулы снят — у всех Эпли. Кнопка в старом сообщении настроек
    ничего не переключает, а перерисовывает экран уже без неё."""
    for data in ("settings:formula", "settings:formulayes", "settings:formulano"):
        state = await _make_state(user_id)
        callback = _make_callback(user_id, data)
        await settings.settings_formula_gone(callback, state)
        assert (await fresh_db.get_user(user_id))["e1rm_formula"] == "epley"


async def test_migration_puts_everyone_on_epley(fresh_db, user_id):
    await fresh_db.update_user(user_id, e1rm_formula="brzycki")
    await fresh_db._reset_e1rm_formula_to_epley()
    assert (await fresh_db.get_user(user_id))["e1rm_formula"] == "epley"


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
    # Четыре поля памяти (дни в неделю тренер больше не запоминает) и пол,
    # пока атлет его не выбрал.
    assert len(empty) == 5
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

    # Профиль: единицы+пояс, язык+пол — парами по 2, короткие подписи.
    # Формулы e1RM нет: у всех Эпли.
    assert rows[0] == ["settings:unit", "settings:tz"]
    assert rows[1] == ["settings:lang", "settings:gender:menu"]
    # Как разговариваю: тумблеры, по одному в ряд (КБЖУ ушёл вместе с AI-разбором еды) — длинные подписи;
    # диапазон повторов — сразу под подсказками прогрессии.
    assert rows[2] == ["settings:progression"]
    assert rows[3] == ["settings:rep_range"]
    assert rows[4] == ["settings:pushes"]
    assert rows[5] == ["settings:ai_comments"]
    # Данные: что тренер знает, экспорт, импорт, MCP, отзыв — тоже по одному.
    assert rows[6] == ["settings:profile"]
    assert rows[7] == ["settings:export"]
    assert rows[8] == ["settings:import"]
    assert rows[9] == ["settings:mcp"]
    assert rows[10] == ["feedback:open"]
    # Сервис для тренеров — кто видит твой дневник (handlers/coach.py).
    assert rows[11] == ["coach:hub"]
    assert rows[12] == ["invite:show"]
    # Снос аккаунта — последним в блоке и отдельной строкой: единственная
    # необратимая кнопка экрана, промахиваться по ней нельзя (Apple 5.1.1(v)).
    assert rows[13] == ["settings:delete"]
    assert rows[14] == ["settings:back"]


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
        "kg", "epley", True, True, True, show_extra_stats=True
    )
    off_kb = keyboards.settings_keyboard(
        "kg", "epley", False, False, False, show_extra_stats=False
    )
    on_texts = [b.text for row in on_kb.inline_keyboard for b in row]
    off_texts = [b.text for row in off_kb.inline_keyboard for b in row]

    assert "🎯 Подсказки прогрессии: подсказываю" in on_texts
    assert "🎯 Подсказки прогрессии: молчу" in off_texts
    assert "🔔 Пуши: шлю" in on_texts
    assert "🔕 Пуши: молчу" in off_texts
    # Выключенный автокомментарий — не «молчу»: комментарий есть, но по кнопке
    # под итогами. Включённый — приходит сам сразу после тренировки.
    assert "🤖 Комментарий тренера: присылаю сразу" in on_texts
    assert "🤖 Комментарий тренера: пишу по кнопке" in off_texts
    # «КБЖУ: считаю» ушёл вместе с AI-разбором еды — считать больше нечем.
    assert not any("КБЖУ" in t for t in on_texts + off_texts)
    # «e1RM на карточке» тумблером больше нет: у всех и всегда.
    assert not any("e1RM на карточке" in t for t in on_texts + off_texts)
    for text in on_texts + off_texts:
        assert len(text) <= 40, f"кнопка слишком длинная для Telegram: {text!r}"


# ---------- экран настроек говорит по-русски, а не телеметрией ----------


async def test_settings_keyboard_shows_russian_unit_names_and_no_formula():
    """«Единицы: kg» — телеметрия, а не реплика тренера (TONE_OF_VOICE.md).
    Внутренние значения ("kg"/"lb") не меняются — русифицируется только то,
    что видит человек. Формулы e1RM на экране нет вовсе: у всех Эпли."""
    import keyboards

    kb = keyboards.settings_keyboard("lb", "brzycki", True, True, True)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("фунты" in label for label in labels)
    assert not any("lb" in label for label in labels)
    assert not any("Бжицки" in label or "Эпли" in label or "Формула" in label for label in labels)

    kb_kg = keyboards.settings_keyboard("kg", "epley", True, True, True)
    labels_kg = [b.text for row in kb_kg.inline_keyboard for b in row]
    assert any("кг" in label for label in labels_kg)


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


async def test_ai_comments_toggle_starts_off_and_says_what_changed(fresh_db, user_id):
    """Новичку автокомментарий выключен: кнопка говорит «по кнопке». Тап
    включает «присылаю сразу», и всплывашка объясняет, что теперь комментарий
    придёт сам сразу после тренировки; обратный тап — что он по кнопке."""
    import i18n

    db = fresh_db
    state = await _make_state(user_id)

    callback = _make_callback(user_id, "menu:settings")
    await settings.show_settings(callback, state)
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert i18n.t_in("ru", "btn.ai_comments_off") in labels

    callback = _make_callback(user_id, "settings:ai_comments")
    await settings.settings_ai_comments(callback, state)
    assert (await db.get_user(user_id))["ai_comments_enabled"] == 1
    callback.answer.assert_awaited_with(i18n.t_in("ru", "settings.ai_comments.toast_on"), show_alert=False)
    kb = callback.message.answer.call_args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert i18n.t_in("ru", "btn.ai_comments_on") in labels

    callback = _make_callback(user_id, "settings:ai_comments")
    await settings.settings_ai_comments(callback, state)
    assert (await db.get_user(user_id))["ai_comments_enabled"] == 0
    callback.answer.assert_awaited_with(i18n.t_in("ru", "settings.ai_comments.toast_off"), show_alert=False)


async def test_card_detail_button_from_old_messages_just_redraws(fresh_db, user_id):
    """Тумблер «e1RM на карточке» снят: кнопка в старом сообщении ничего не
    выключает, а перерисовывает настройки."""
    state = await _make_state(user_id)
    callback = _make_callback(user_id, "settings:card_detail")
    await settings.settings_card_detail_gone(callback, state)
    assert (await fresh_db.get_user(user_id))["show_extra_stats"] == 1


async def test_migration_turns_extra_stats_on_for_everyone(fresh_db, user_id):
    await fresh_db.update_user(user_id, show_extra_stats=0)
    await fresh_db._show_extra_stats_for_everyone()
    assert (await fresh_db.get_user(user_id))["show_extra_stats"] == 1
