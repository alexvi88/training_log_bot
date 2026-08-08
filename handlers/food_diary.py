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

import ai_trainer
import db
import formatting
import keyboards
import state_scaffold
import timeutil
import ui
from fsm import FoodDiaryFlow

router = Router(name="food_diary")

logger = logging.getLogger(__name__)

# Телеграмовское фото и так пережато, но подпирать base64-раздутым мегабайтником
# запрос к модели незачем — тот же порог, что у фото-вопросов AI-тренеру.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_ADD_HINT = (
    "🍽 Напиши, что съел, или пришли фото еды (можно с подписью) — "
    "прикину калории и БЖУ, а ты подтвердишь."
)

_CORRECT_HINT = (
    "✏️ Напиши, что не так — например «это была груша, и порция граммов 300» "
    "или «добавь ещё кофе с молоком»."
)

_NOT_FOOD_HINT = "Пришли фото самой еды или напиши текстом, что съел."

# Пока модель считает — чтобы экран не выглядел зависшим.
_THINKING_TEXT = "🤔 Разбираю, что тут..."


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
    rows = await db.list_food_entries(user_id, date.isoformat())
    entries = _entry_views(rows)
    text = formatting.build_food_day_screen(date, entries)
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
    await db.get_or_create_user(message.from_user.id, message.from_user.username)
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
        await message.reply(
            "🤖 Распознавание еды пока не настроено на сервере — попробуй позже."
        )
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
    placeholder = await message.answer(_THINKING_TEXT)
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
            await placeholder.edit_text(
                "⚠️ Не получилось разобрать, что это за еда. Попробуй ещё раз "
                "или напиши текстом, что съел."
            )
        # Возвращаемся в режим просмотра дня, не трогая сам экран дня — он не
        # удалялся и не менялся, перерисовывать (и тем более удалять) нечего.
        await state.set_state(FoodDiaryFlow.viewing)
        await state.update_data(fd_pending=None)
        return

    if not estimate.get("is_food", True):
        # Модель уверенно говорит, что это не еда — не подсовываем «Всё верно?»
        # с нечем подтверждать: карточка на пустом месте только злит (см. отчёт
        # пользователя про «нахуя мне заносить»). Просто объясняем и возвращаем
        # экран дня, ничего не сохраняя.
        comment = estimate.get("comment", "").strip()
        not_food_text = "🤔 Не нашёл тут еды" + (f": {escape(comment)}" if comment else ".")
        not_food_text += f"\n{_NOT_FOOD_HINT}"
        with suppress(TelegramBadRequest):
            await placeholder.edit_text(not_food_text)
        await state.set_state(FoodDiaryFlow.viewing)
        await state.update_data(fd_pending=None)
        return

    if not estimate.get("description"):
        # Модель не поняла, что на фото/в тексте — подставляем то, что написал
        # человек, чтобы запись всё равно можно было сохранить своими словами.
        estimate["description"] = text.strip() or "Приём пищи"

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
        await message.reply("Фото слишком большое, пришли поменьше.")
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
    await _analyze_and_show(message, state, text=text)


# ---------- подтверждение и правка ----------


@router.callback_query(StateFilter(FoodDiaryFlow.confirming), F.data == "fd:fix")
async def fd_fix(callback: CallbackQuery, state: FSMContext):
    """Догадка модели остаётся на экране — правку проще написать, глядя на то,
    что именно нужно поправить, а не вслепую по памяти."""
    await state.set_state(FoodDiaryFlow.correcting)
    data = await state.get_data()
    pending = data.get("fd_pending") or {}
    text = _estimate_text(pending) + "\n\n" + _CORRECT_HINT
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
    await callback.answer("Отменил")


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
        pending.get("description") or "Приём пищи", formatting.FOOD_DESC_LIMIT
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
    data = await state.get_data()
    pending = data.get("fd_pending")
    if not pending:
        await callback.answer("Нечего сохранять", show_alert=True)
        return
    await _save_now(callback, state, pending)
    await callback.answer("Записал 👌")


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
        await callback.answer("Запись не найдена", show_alert=True)
        return
    date = dt.date.fromisoformat(entry["eaten_on"])
    await ui.safe_edit(
        callback,
        f"Удалить «{escape(entry['description'])}»?",
        reply_markup=keyboards.food_delete_confirm_keyboard(entry_id, date),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fd:del:"))
async def fd_delete(callback: CallbackQuery, state: FSMContext):
    entry_id = int(callback.data.split(":")[2])
    entry = await db.get_food_entry(entry_id)
    if entry is None or entry["telegram_id"] != callback.from_user.id:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await db.delete_food_entry(entry_id)
    await _show_day(callback, state, dt.date.fromisoformat(entry["eaten_on"]))
    await callback.answer("Удалил")


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
