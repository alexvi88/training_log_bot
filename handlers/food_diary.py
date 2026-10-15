"""🍽 Дневник питания — что съел, по дням. Без модели: что написал, то и записано.

Открывается кнопкой «🍽 Дневник еды» в главном меню или командой /food_diary.

Как устроен ввод. Пользователь на экране дня просто пишет текстом или шлёт фото
(можно фото с подписью) — отдельной кнопки «добавить» не нужно, как и на экране
веса тела. Запись ложится сразу, как есть: AI-разбора еды (калории по фото,
КБЖУ моделью) больше нет — он выпилен целиком.

Дата не спрашивается отдельным шагом: запись уходит в тот день, что сейчас
открыт на экране. Чтобы занести еду за прошлый день, сначала переключаются на
него стрелками/«История» на самом экране дня, а потом уже пишут/шлют фото —
один и тот же путь что для сегодня, что для прошлого.
"""

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

import config
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


# Границы вынесены в config.py — их же использует POST /food/goal в REST API,
# чтобы предел не разъехался между ботом и приложением.
KCAL_GOAL_MIN = config.KCAL_GOAL_MIN
KCAL_GOAL_MAX = config.KCAL_GOAL_MAX


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


# ---------- запись ----------
#
# Без модели: AI-разбор еды выпилен целиком (в приложении дневника нет вовсе,
# тренеру записи еды не уходят). Что человек написал — то и ложится в день;
# фото сохраняется как file_id с подписью (или без неё — тогда «Приём пищи»).


# Роутер дневника подключён раньше workout.router (чтобы /food_diary долетал из
# любого состояния), поэтому его текстовые хендлеры обязаны пропускать команды
# дальше по цепочке — иначе /start, набранный на экране дня, лёг бы в дневник
# как «что съел». Не совпавший фильтр отдаёт апдейт следующему роутеру, в
# отличие от раннего return внутри хендлера.
_NOT_A_COMMAND = ~F.text.startswith("/")


def _plain_text_pending(text: str) -> dict[str, Any]:
    """Пищевая запись — то, что человек написал, как есть."""
    return {
        "is_food": True, "description": text, "items": [], "calories": None,
        "protein": None, "fat": None, "carbs": None, "comment": "",
        "photo_file_id": None, "source": "text",
    }


@router.message(StateFilter(FoodDiaryFlow.viewing), F.photo)
async def fd_photo_entry(message: Message, state: FSMContext):
    caption = (message.caption or "").strip()
    pending = _plain_text_pending(caption)
    pending["photo_file_id"] = message.photo[-1].file_id
    pending["source"] = "photo_text" if caption else "photo"
    await _save_now(message, state, pending)


@router.message(StateFilter(FoodDiaryFlow.viewing), F.text, _NOT_A_COMMAND)
async def fd_text_entry(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        return
    await _save_now(message, state, _plain_text_pending(text))


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
