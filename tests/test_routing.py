"""Кто из роутеров забирает апдейт — это поведение, а не деталь сборки.

Порядок include_router уже ломал шаринг: у workout.router обычный
Command("start"), который матчит и "/start sh_<token>" из визитки, так что
зарегистрированный после него sharing.router не получал deep link вообще —
получатель просто попадал в главное меню, ничего не добавив.
"""
import datetime as dt
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

import main

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    """Роутеры — модульные синглтоны и прикрепляются к диспетчеру ровно один
    раз, поэтому собираем его на весь модуль, а не на тест."""
    dp = Dispatcher()
    main.setup_routers(dp)
    return dp


async def _feed(dp: Dispatcher, text: str) -> list[str]:
    """Прогнать сообщение через маршрутизацию и вернуть, кто его забрал.

    Хендлеры на время прогона подменяются заглушками (нам нужен победитель, а
    не его поход в БД и Telegram) и обязательно возвращаются на место: роутеры
    общие на весь процесс, и утёкшая подмена сломала бы остальные тесты.
    """
    winners: list[str] = []
    originals: list[tuple[object, object]] = []
    for router in [dp, *dp.sub_routers]:
        for handler in router.message.handlers:
            callback = handler.callback
            originals.append((handler, callback))

            def make(callback=callback):
                async def spy(*args, **kwargs):
                    winners.append(f"{callback.__module__}.{callback.__name__}")

                return spy

            handler.callback = make()

    bot = Bot(token="42:TEST")
    bot.session = AsyncMock()
    message = Message(
        message_id=1,
        date=dt.datetime.now(),
        chat=Chat(id=555, type="private"),
        from_user=User(id=555, is_bot=False, first_name="Recipient"),
        text=text,
    ).as_(bot)
    try:
        await dp.feed_update(bot, Update(update_id=1, message=message))
    finally:
        for handler, callback in originals:
            handler.callback = callback
    return winners


async def test_share_deep_link_reaches_sharing_not_the_main_menu(dispatcher):
    """Регрессия: "/start sh_…" должен открывать превью присланной программы."""
    assert await _feed(dispatcher, "/start sh_TOKEN123") == ["handlers.sharing.open_shared"]


async def test_plain_start_still_opens_the_main_menu(dispatcher):
    """При этом обычный /start обязан остаться за workout.cmd_start —
    лечение не должно увести к шарингу всех подряд."""
    assert await _feed(dispatcher, "/start") == ["handlers.workout.cmd_start"]


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_no_callback_can_fall_through_the_routers(dispatcher):
    """Регрессия на «бот залип»: перехватчик кнопок обязан существовать и обязан
    стоять последним.

    Существовать — потому что больше сотни обработчиков стоят под `StateFilter`,
    и после `state.clear()` их callback не брал никто: Telegram крутил спиннер и
    гасил его молча, без ответа, без ошибки и без строчки в логах. Последним —
    потому что без фильтров он съел бы и те кнопки, у которых обработчик есть.
    """
    assert [r.name for r in dispatcher.sub_routers][-1] == "fallback"
    catch_all = [
        h
        for router in dispatcher.sub_routers
        for h in router.callback_query.handlers
        if not h.filters
    ]
    assert catch_all, "нет перехватчика callback_query — кнопки уйдут в тишину"
