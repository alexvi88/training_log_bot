"""🍽 Дневник питания — что съел, по дням, с распознаванием еды моделью.

Открывается кнопкой «🍽 Дневник еды» в главном меню или командой /food_diary.

Как устроен ввод. Пользователь на экране дня просто пишет текстом или шлёт фото
(можно фото с подписью) — отдельной кнопки «добавить» не нужно, как и на экране
веса тела. Всё, что пришло, уходит в ai_trainer.analyze_food, оттуда возвращается
структурированная оценка (название, раскладка, ккал и БЖУ), и она показывается
как карточка с вопросом «всё верно?». Пользователь подтверждает, правит словами
(«это была груша, и порция 300 г» — правка уходит модели вместе с прошлой
догадкой, а не начинает разбор с нуля) или отменяет.

Дата не спрашивается отдельным шагом: запись уходит в тот день, что сейчас
открыт на экране. Чтобы занести еду за прошлый день, сначала переключаются на
него стрелками/«История» на самом экране дня, а потом уже пишут/шлют фото —
один и тот же путь что для сегодня, что для прошлого.
"""

import asyncio
import base64
import datetime as dt
import json
import logging
from contextlib import suppress
from html import escape
from typing import Any, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import ai_limits
import ai_trainer
import db
import formatting
import i18n
import keyboards
import state_scaffold
import timeutil
import ui
from fsm import FoodDiaryFlow

router = Router(name="food_diary")

logger = logging.getLogger(__name__)

# Guards "fd:ok" against a double tap: fd_pending is only cleared inside
# _show_day, after db.add_food_entry has already run — and two callbacks from
# the same user can run concurrently, each reading its own snapshot of state
# with fd_pending still set, so the second tap would save the same meal a
# second time. Same shape as handlers.workout._confirming / _try_claim_weight_confirm.
_confirming: set[int] = set()


def _try_claim_confirming(user_id: int) -> bool:
    """Atomically check-and-reserve `_confirming` for this user — no `await`
    between the membership check and the `.add()`, same reasoning as
    ai_trainer._try_claim_busy."""
    if user_id in _confirming:
        return False
    _confirming.add(user_id)
    return True

# Телеграмовское фото и так пережато, но подпирать base64-раздутым мегабайтником
# запрос к модели незачем — тот же порог, что у фото-вопросов AI-тренеру.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


# ---------- вспомогательное ----------


async def _today(user_id: int) -> dt.date:
    return timeutil.user_today(await db.get_user(user_id))


async def _state_date(state: FSMContext, user_id: int) -> dt.date:
    """Просматриваемый день — то, что лежит в FSM, иначе сегодняшний."""
    data = await state.get_data()
    stored = data.get("fd_date")
    if stored:
        with suppress(ValueError):
            return dt.date.fromisoformat(stored)
    return await _today(user_id)


def _parse_items(raw: Optional[str]) -> list[formatting.FoodItemView]:
    """`food_entries.details` holds the per-product breakdown as JSON — see
    `_items_to_json`. Empty/broken JSON just means no breakdown to show."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [
        formatting.FoodItemView(
            name=i.get("name", ""), portion=i.get("portion", ""),
            calories=i.get("calories"), protein=i.get("protein"),
            fat=i.get("fat"), carbs=i.get("carbs"),
        )
        for i in data if isinstance(i, dict) and i.get("name")
    ]


def _items_to_json(items: Optional[list[dict[str, Any]]]) -> Optional[str]:
    return json.dumps(items, ensure_ascii=False) if items else None


def _entry_views(rows) -> list[formatting.FoodEntryView]:
    return [
        formatting.FoodEntryView(
            id=r["id"],
            description=r["description"],
            items=_parse_items(r["details"]),
            calories=r["calories"],
            protein=r["protein"],
            fat=r["fat"],
            carbs=r["carbs"],
            has_photo=bool(r["photo_file_id"]),
        )
        for r in rows
    ]


async def _clear_previous_screen(message: Message, state: FSMContext) -> None:
    """Убрать прошлый экран дневника, чтобы ввод не оставлял за собой ленту из
    одинаковых экранов (тот же приём, что в handlers/bodyweight.py)."""
    data = await state.get_data()
    screen_id = data.get("fd_screen_id")
    if screen_id is None:
        return
    with suppress(TelegramBadRequest):
        await message.bot.delete_message(chat_id=message.chat.id, message_id=screen_id)
    await state.update_data(fd_screen_id=None)


async def _show_day(event, state: FSMContext, date: dt.date) -> None:
    """Отрисовать экран одного дня и встать в состояние его просмотра."""
    user_id = event.from_user.id
    user = await db.get_user(user_id)
    rows = await db.list_food_entries(user_id, date.isoformat())
    entries = _entry_views(rows)
    text = formatting.build_food_day_screen(date, entries, kcal_goal=user["kcal_goal"])
    kb = keyboards.food_day_keyboard(date, [e.id for e in entries], today=await _today(user_id))

    await state.set_state(FoodDiaryFlow.viewing)
    await state.update_data(fd_date=date.isoformat(), fd_pending=None)

    if isinstance(event, CallbackQuery):
        sent = await ui.safe_edit(event, text, reply_markup=kb, parse_mode="HTML")
    else:
        await _clear_previous_screen(event, state)
        sent = await event.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.update_data(fd_screen_id=getattr(sent, "message_id", None))


# ---------- вход в раздел ----------


@router.message(Command("food_diary"))
async def cmd_food_diary(message: Message, state: FSMContext):
    await db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.language_code
    )
    # Записать еду можно и в перерыве между подходами: сбрасываем поток, но не
    # каркас незакрытой тренировки — иначе возврат в трекер покажет пустой экран.
    await state_scaffold.clear_state_keep_workout(state)
    await _show_day(message, state, await _today(message.from_user.id))


@router.callback_query(F.data == "menu:food")
async def menu_food_diary(callback: CallbackQuery, state: FSMContext):
    # То же, что и в /food_diary: кнопка меню — это не конец тренировки.
    await state_scaffold.clear_state_keep_workout(state)
    await _show_day(callback, state, await _today(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("fd:day:"))
async def fd_open_day(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.split(":", 2)[2]
    if raw == "today":
        date = await _today(callback.from_user.id)
    else:
        date = dt.date.fromisoformat(raw)
    await _show_day(callback, state, date)
    await callback.answer()


@router.callback_query(F.data == "fd:menu")
async def fd_menu(callback: CallbackQuery, state: FSMContext):
    from handlers.workout import _show_main_menu

    # Состояние снимает сам _show_main_menu, и снимает бережно (каркас открытой
    # тренировки остаётся). Стоявший здесь state.clear() успевал снести каркас до
    # него — и «Продолжить» в меню открывало тренировку без упражнений.
    await _show_main_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == "fd:noop")
async def fd_noop(callback: CallbackQuery):
    await callback.answer()


# Разумные границы дневной цели — просто чтобы не записать в базу опечатку
# («22000» вместо «2200»), а не диетологический лимит.
KCAL_GOAL_MIN = 500
KCAL_GOAL_MAX = 10000


@router.callback_query(F.data == "fd:goal")
async def fd_goal_prompt(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    date = data.get("fd_date")
    await state.set_state(FoodDiaryFlow.setting_goal)
    back_cb = f"fd:day:{date}" if date else "fd:menu"
    await ui.safe_edit(
        callback, i18n.t("food.goal_prompt"), reply_markup=keyboards.cancel_keyboard(back_cb)
    )
    await callback.answer()


@router.message(StateFilter(FoodDiaryFlow.setting_goal), F.text)
async def fd_goal_entered(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ", "")
    if not raw.isdigit():
        await ui.reply_transient(message, i18n.t("food.goal_invalid"))
        return
    goal = int(raw)
    if not (KCAL_GOAL_MIN <= goal <= KCAL_GOAL_MAX):
        await ui.reply_transient(message, i18n.t("food.goal_out_of_range", max=KCAL_GOAL_MAX))
        return
    await db.set_kcal_goal(message.from_user.id, goal)
    await message.reply(i18n.t("food.goal_saved", goal=goal))
    data = await state.get_data()
    date = dt.date.fromisoformat(data["fd_date"]) if data.get("fd_date") else await _today(message.from_user.id)
    await _show_day(message, state, date)


# ---------- распознавание еды ----------


async def _download_photo_as_data_url(message: Message) -> Optional[str]:
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        return None
    buf = await message.bot.download(photo)
    return "data:image/jpeg;base64," + base64.b64encode(buf.read()).decode()


def _estimate_text(estimate: dict[str, Any]) -> str:
    items = [
        formatting.FoodItemView(
            name=i.get("name", ""), portion=i.get("portion", ""),
            calories=i.get("calories"), protein=i.get("protein"),
            fat=i.get("fat"), carbs=i.get("carbs"),
        )
        for i in estimate.get("items") or []
    ]
    return formatting.build_food_estimate_text(
        estimate.get("description", ""),
        items,
        calories=estimate.get("calories"),
        protein=estimate.get("protein"),
        fat=estimate.get("fat"),
        carbs=estimate.get("carbs"),
        comment=estimate.get("comment", ""),
    )


async def _show_estimate(
    message: Message,
    state: FSMContext,
    estimate: dict[str, Any],
    placeholder: Optional[Message] = None,
) -> None:
    """Показать догадку модели с вопросом «всё верно?» и запомнить её в FSM.

    Запись создаётся только после подтверждения, поэтому до него оценка живёт
    в состоянии, а не в БД: отменённый или переигранный разбор не должен
    оставлять следов в дневнике.
    """
    await state.update_data(fd_pending=estimate)
    await state.set_state(FoodDiaryFlow.confirming)
    text = _estimate_text(estimate)
    kb = keyboards.food_confirm_keyboard()
    if placeholder is not None:
        try:
            await placeholder.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass  # экран мог не пережить правку — просто шлём новым сообщением
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _analyze_and_show(
    message: Message,
    state: FSMContext,
    text: str = "",
    image_data_url: Optional[str] = None,
    photo_file_id: Optional[str] = None,
    previous: Optional[dict[str, Any]] = None,
    correction: str = "",
) -> None:
    if not ai_trainer.is_configured():
        await message.reply(i18n.t("food.not_configured"))
        return

    # Единственная платная поверхность бота, которая раньше не считалась вовсе:
    # вопросы, видео и поиск свои квоты имели, а фотографировать тарелку можно
    # было бесконечно. Проверяем здесь, потому что сюда сходятся все три входа —
    # фото, текст и правка «✏️ Поправить».
    block = await ai_limits.check(message.from_user.id, ai_limits.KIND_FOOD)
    if block is not None:
        logger.info("food analysis blocked for user %s: %s", message.from_user.id, block.log)
        await ai_limits.reply(message, block)
        # preview — свой аккаунт, ещё не нажавший «Понятно» сегодня: разбор
        # всё равно идёт, предупреждение не отменяет то, что его вызвало.
        if not block.preview:
            await state.set_state(FoodDiaryFlow.viewing)
            return

    # Модели отдаём только пищевую часть прошлой догадки: file_id фотографии,
    # признак источника и вердикт is_food ей ни о чём не говорят, а место в
    # промпте занимают. Само фото при правке заново не пересылается — держать
    # base64 картинки в FSM (а он пишется на диск) дороже, чем пересчитать
    # оценку по прошлой раскладке плюс правке пользователя.
    model_previous = (
        {k: v for k, v in previous.items() if k not in ("photo_file_id", "source", "is_food")}
        if previous
        else None
    )

    user = await db.get_user(message.from_user.id)
    with_macros = bool(user["food_macros_enabled"])

    # Экран дня (с подсказкой «напиши, что съел») намеренно не удаляется —
    # разбор идёт отдельным сообщением ниже, а не заменяет собой то, на что
    # человек только что ответил.
    placeholder = await message.answer(i18n.t("food.thinking"))
    try:
        estimate = await asyncio.wait_for(
            ai_trainer.analyze_food(
                message.from_user.id,
                text=text,
                image_data_url=image_data_url,
                previous=model_previous,
                correction=correction,
                with_macros=with_macros,
            ),
            timeout=90,
        )
    except Exception:
        logger.exception("food analysis failed for user %s", message.from_user.id)
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(i18n.t("food.analysis_failed"))
        # Возвращаемся в режим просмотра дня, не трогая сам экран дня — он не
        # удалялся и не менялся, перерисовывать (и тем более удалять) нечего.
        await state.set_state(FoodDiaryFlow.viewing)
        await state.update_data(fd_pending=None)
        return

    # Квота тратится за состоявшийся разбор — как и у вопросов с видео: сбой
    # провайдера не должен стоить человеку попытки. Списывается и за «это не
    # еда», и за правку: платный вызов уже сделан, и деньгам всё равно, чем он
    # кончился.
    await db.increment_ai_food_count(message.from_user.id)

    if not estimate.get("is_food", True):
        # Модель уверенно говорит, что это не еда — не подсовываем «Всё верно?»
        # с нечем подтверждать: карточка на пустом месте только злит (см. отчёт
        # пользователя про «нахуя мне заносить»). Просто объясняем и возвращаем
        # экран дня, ничего не сохраняя.
        comment = estimate.get("comment", "").strip()
        not_food_text = i18n.t("food.not_food_detected") + (f": {escape(comment)}" if comment else ".")
        not_food_text += f"\n{i18n.t('food.not_food_hint')}"
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(not_food_text)
        await state.set_state(FoodDiaryFlow.viewing)
        await state.update_data(fd_pending=None)
        return

    if not estimate.get("description"):
        # Модель не поняла, что на фото/в тексте — подставляем то, что написал
        # человек, чтобы запись всё равно можно было сохранить своими словами.
        estimate["description"] = text.strip() or i18n.t("food.default_meal_name")

    # Фото не уходит в БД целиком: file_id хватает, чтобы показать 📷 в дневнике
    # и (позже) переслать снимок, а картинки Telegram хранит у себя.
    estimate["photo_file_id"] = photo_file_id or (previous or {}).get("photo_file_id")
    estimate["source"] = (
        "photo_text" if estimate["photo_file_id"] and text else
        "photo" if estimate["photo_file_id"] else "text"
    )
    await _show_estimate(message, state, estimate, placeholder=placeholder)


@router.message(StateFilter(FoodDiaryFlow.viewing), F.photo)
async def fd_photo_entry(message: Message, state: FSMContext):
    image_data_url = await _download_photo_as_data_url(message)
    if image_data_url is None:
        await message.reply(i18n.t("food.photo_too_large", mb=MAX_IMAGE_BYTES // (1024 * 1024)))
        return
    await _analyze_and_show(
        message,
        state,
        text=(message.caption or "").strip(),
        image_data_url=image_data_url,
        photo_file_id=message.photo[-1].file_id,
    )


# Роутер дневника подключён раньше workout.router (чтобы /food_diary долетал из
# любого состояния), поэтому его текстовые хендлеры обязаны пропускать команды
# дальше по цепочке — иначе /start, набранный на экране дня, ушёл бы модели как
# «что съел». Не совпавший фильтр отдаёт апдейт следующему роутеру, в отличие от
# раннего return внутри хендлера.
_NOT_A_COMMAND = ~F.text.startswith("/")


def _plain_text_pending(text: str) -> dict[str, Any]:
    """Пищевая запись без модели — то, что человек написал, как есть.

    Используется, когда КБЖУ выключен в настройках: для текста это вообще не
    требует запроса к модели — сохранять нечего проверять, кроме собственных
    слов пользователя.
    """
    return {
        "is_food": True, "description": text, "items": [], "calories": None,
        "protein": None, "fat": None, "carbs": None, "comment": "",
        "photo_file_id": None, "source": "text",
    }


@router.message(StateFilter(FoodDiaryFlow.viewing), F.text, _NOT_A_COMMAND)
async def fd_text_entry(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        return
    user = await db.get_user(message.from_user.id)
    if not user["food_macros_enabled"]:
        # «Просто сохраняет твой текст» — без карточки-подтверждения: тут
        # нечего проверять, это уже ровно то, что человек написал.
        await _save_now(message, state, _plain_text_pending(text))
        return

    # Кончилась квота разборов — дневник не запирается: текст сохраняется как
    # есть, тем же путём, что при выключенном КБЖУ. Пейволл на собственную еду —
    # ровно то, за что нас понесли бы в отзывы (MONETIZATION.md), а разница для
    # человека тут одна: сегодня без калорий.
    block = await ai_limits.check(message.from_user.id, ai_limits.KIND_FOOD)
    if block is not None:
        if block.preview:
            # Свой аккаунт, ещё не нажавший «Понятно» сегодня: разбор всё
            # равно идёт — предупреждение не отменяет то, что его вызвало.
            await ai_limits.reply(message, block)
        else:
            await message.reply(i18n.t("food.saved_plain"))
            await _save_now(message, state, _plain_text_pending(text))
            return

    await _analyze_and_show(message, state, text=text)


# ---------- подтверждение и правка ----------


@router.callback_query(StateFilter(FoodDiaryFlow.confirming), F.data == "fd:fix")
async def fd_fix(callback: CallbackQuery, state: FSMContext):
    """Догадка модели остаётся на экране — правку проще написать, глядя на то,
    что именно нужно поправить, а не вслепую по памяти."""
    await state.set_state(FoodDiaryFlow.correcting)
    data = await state.get_data()
    pending = data.get("fd_pending") or {}
    text = _estimate_text(pending) + "\n\n" + i18n.t("food.correct_hint")
    await ui.safe_edit(
        callback, text, reply_markup=keyboards.cancel_keyboard("fd:cancel"), parse_mode="HTML"
    )
    await callback.answer()


@router.message(
    StateFilter(FoodDiaryFlow.correcting, FoodDiaryFlow.confirming), F.text, _NOT_A_COMMAND
)
async def fd_correction(message: Message, state: FSMContext):
    """Правка словами — и по кнопке «✏️ Поправить», и просто набранная в ответ
    на карточку: дописать «это была груша» естественнее, чем сперва искать кнопку."""
    correction = (message.text or "").strip()
    if not correction:
        return
    data = await state.get_data()
    pending = data.get("fd_pending") or {}
    await _analyze_and_show(
        message,
        state,
        text=pending.get("description", ""),
        previous=pending,
        correction=correction,
    )


@router.message(StateFilter(FoodDiaryFlow.correcting, FoodDiaryFlow.confirming), F.photo)
async def fd_correction_photo(message: Message, state: FSMContext):
    """Прислать вместо правки другое фото — это тоже правка: разбираем заново."""
    await fd_photo_entry(message, state)


@router.callback_query(
    StateFilter(FoodDiaryFlow.confirming, FoodDiaryFlow.correcting), F.data == "fd:cancel"
)
async def fd_cancel(callback: CallbackQuery, state: FSMContext):
    date = await _state_date(state, callback.from_user.id)
    await state.update_data(fd_pending=None)
    await _show_day(callback, state, date)
    await callback.answer(i18n.t("food.cancelled_toast"))


async def _save_now(event, state: FSMContext, pending: dict[str, Any]) -> None:
    """Сохранить запись в день, который сейчас открыт на экране, и сразу
    показать обновлённый день — без отдельного вопроса «за какую дату».
    Занести за прошлый день можно, переключившись на него ДО ввода еды —
    тем же путём, что и просмотр (стрелки, «История»)."""
    user_id = event.from_user.id
    date = await _state_date(state, user_id)
    # Название режем на входе, а не только при отрисовке: при выключенном КБЖУ
    # сюда попадает текст пользователя целиком (до 4096 символов), и он потом
    # ходит и в экран дня, и в промпт модели.
    description = formatting.shorten(
        pending.get("description") or i18n.t("food.default_meal_name"), formatting.FOOD_DESC_LIMIT
    )
    await db.add_food_entry(
        user_id,
        eaten_on=date.isoformat(),
        description=description,
        details=_items_to_json(pending.get("items")),
        calories=pending.get("calories"),
        protein=pending.get("protein"),
        fat=pending.get("fat"),
        carbs=pending.get("carbs"),
        photo_file_id=pending.get("photo_file_id"),
        source=pending.get("source", "text"),
    )
    await _show_day(event, state, date)


@router.callback_query(StateFilter(FoodDiaryFlow.confirming), F.data == "fd:ok")
async def fd_confirm(callback: CallbackQuery, state: FSMContext):
    """Claims `_confirming` before the first `await`: two fast taps on
    «Записал 👌» both read `fd_pending` while it's still set (each holds its
    own snapshot), so without this guard both would call db.add_food_entry —
    clearing fd_pending only stops a *later* tap, not a second one racing the
    first."""
    user_id = callback.from_user.id
    if not _try_claim_confirming(user_id):
        await callback.answer()
        return
    try:
        data = await state.get_data()
        pending = data.get("fd_pending")
        if not pending:
            await callback.answer(i18n.t("food.nothing_to_save"), show_alert=True)
            return
        await _save_now(callback, state, pending)
        await callback.answer(i18n.t("food.logged_toast"))
    finally:
        _confirming.discard(user_id)


# ---------- удаление и история ----------


@router.callback_query(F.data.startswith("fd:delask:"))
async def fd_delete_ask(callback: CallbackQuery, state: FSMContext):
    """Без StateFilter нарочно: это кнопка на уже отправленном сообщении, а не
    перехватчик текста. Экран дневника питания живёт в чате сколько угодно —
    человек мог за это время уйти в 🤖 AI-тренер и вернуться, и тап по старой
    кнопке не должен упираться в «эта кнопка уже отработала своё» только
    потому, что состояние успело смениться (та же причина, что у rt:pgmdelask
    в handlers/routines.py)."""
    entry_id = int(callback.data.split(":")[2])
    entry = await db.get_food_entry(entry_id)
    if entry is None or entry["telegram_id"] != callback.from_user.id:
        await callback.answer(i18n.t("food.entry_not_found"), show_alert=True)
        return
    date = dt.date.fromisoformat(entry["eaten_on"])
    await ui.safe_edit(
        callback,
        i18n.t("food.delete_confirm", name=escape(entry["description"])),
        reply_markup=keyboards.food_delete_confirm_keyboard(entry_id, date),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fd:del:"))
async def fd_delete(callback: CallbackQuery, state: FSMContext):
    entry_id = int(callback.data.split(":")[2])
    entry = await db.get_food_entry(entry_id)
    if entry is None or entry["telegram_id"] != callback.from_user.id:
        await callback.answer(i18n.t("food.entry_not_found"), show_alert=True)
        return
    await db.delete_food_entry(entry_id)
    await _show_day(callback, state, dt.date.fromisoformat(entry["eaten_on"]))
    await callback.answer(i18n.t("food.deleted_toast"))


@router.callback_query(F.data.startswith("fd:history:"))
async def fd_history(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    size = keyboards.FOOD_HISTORY_PAGE_SIZE
    total = await db.count_food_days(user_id)
    rows = await db.list_food_days(user_id, limit=size, offset=page * size)
    days = [
        formatting.FoodDayView(
            date=dt.date.fromisoformat(r["eaten_on"]), entries=r["entries"], calories=r["calories"],
            protein=r["protein"], fat=r["fat"], carbs=r["carbs"],
            descriptions=(r["descriptions"] or "").split("\n"),
        )
        for r in rows
    ]

    await state.set_state(FoodDiaryFlow.browsing_history)
    sent = await ui.safe_edit(
        callback,
        formatting.build_food_history_list(days),
        reply_markup=keyboards.food_history_keyboard(
            [d.date for d in days], page, has_next=(page + 1) * size < total
        ),
        parse_mode="HTML",
    )
    await state.update_data(fd_screen_id=getattr(sent, "message_id", None))
    await callback.answer()
