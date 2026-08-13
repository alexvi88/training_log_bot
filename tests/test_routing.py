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
from aiogram.fsm.context import FSMContext
from aiogram.types import Chat, Message, MessageOriginChannel, Update, User

import main
from fsm import AITrainerFlow, FeedbackFlow

pytestmark = pytest.mark.asyncio

# Тот, от чьего имени приходят сообщения в _feed, — вынесено, чтобы по этому же
# ключу доставать его состояние из хранилища диспетчера.
_CHAT_ID = 555
_BOT_TOKEN = "42:TEST"


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    """Роутеры — модульные синглтоны и прикрепляются к диспетчеру ровно один
    раз, поэтому собираем его на весь модуль, а не на тест."""
    dp = Dispatcher()
    main.setup_routers(dp)
    return dp


async def _feed(dp: Dispatcher, text: str, *, forward_origin=None) -> list[str]:
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

    bot = Bot(token=_BOT_TOKEN)
    bot.session = AsyncMock()
    message = Message(
        message_id=1,
        date=dt.datetime.now(),
        chat=Chat(id=_CHAT_ID, type="private"),
        from_user=User(id=_CHAT_ID, is_bot=False, first_name="Recipient"),
        text=text,
        forward_origin=forward_origin,
    ).as_(bot)
    try:
        await dp.feed_update(bot, Update(update_id=1, message=message))
    finally:
        for handler, callback in originals:
            handler.callback = callback
    return winners


def _fsm(dispatcher: Dispatcher) -> FSMContext:
    """Состояние того же собеседника, чьи сообщения шлёт _feed: ключ хранилища
    диспетчер собирает из id бота и чата, а они у нас всегда одни и те же."""
    return dispatcher.fsm.resolve_context(
        bot=Bot(token=_BOT_TOKEN), chat_id=_CHAT_ID, user_id=_CHAT_ID
    )


async def test_start_typed_while_writing_feedback_opens_the_menu(dispatcher):
    """Регрессия: /start передумавшего уходил админу как текст отзыва.

    Роутер фидбека подключён вторым, поэтому его «ловлю всё» состояние стояло
    впереди команд остальных роутеров — и выхода из отзыва не было вовсе.
    """
    fsm = _fsm(dispatcher)
    await fsm.set_state(FeedbackFlow.awaiting_message)
    try:
        assert await _feed(dispatcher, "/start") == ["handlers.workout.cmd_start"]
        # И заодно человек больше не в отзыве: следующая его реплика — не отзыв.
        assert await fsm.get_state() is None
    finally:
        await fsm.clear()


async def test_unknown_command_also_ends_the_feedback_flow(dispatcher):
    """«/cancel», «/menu» — команды, которых у бота нет: их забирает fallback, и
    сам он состояний не снимает. Снять его всё равно надо, иначе следующая
    реплика («а сколько мне есть белка?») опять уедет админу."""
    fsm = _fsm(dispatcher)
    await fsm.set_state(FeedbackFlow.awaiting_message)
    try:
        assert await _feed(dispatcher, "/cancel") == ["handlers.fallback.unhandled_text"]
        assert await fsm.get_state() is None
    finally:
        await fsm.clear()


async def test_plain_text_in_the_feedback_flow_still_reaches_the_admin(dispatcher):
    """Обратная сторона: лечение не должно перестать принимать сами отзывы."""
    fsm = _fsm(dispatcher)
    await fsm.set_state(FeedbackFlow.awaiting_message)
    try:
        assert await _feed(dispatcher, "кнопка веса не нажимается") == [
            "handlers.feedback.feedback_message"
        ]
        assert await fsm.get_state() == FeedbackFlow.awaiting_message.state
    finally:
        await fsm.clear()


async def test_share_deep_link_reaches_sharing_not_the_main_menu(dispatcher):
    """Регрессия: "/start sh_…" должен открывать превью присланной программы."""
    assert await _feed(dispatcher, "/start sh_TOKEN123") == ["handlers.sharing.open_shared"]


async def test_plain_start_still_opens_the_main_menu(dispatcher):
    """При этом обычный /start обязан остаться за workout.cmd_start —
    лечение не должно увести к шарингу всех подряд."""
    assert await _feed(dispatcher, "/start") == ["handlers.workout.cmd_start"]


async def test_forwarded_post_reaches_factcheck_even_mid_ai_chat(dispatcher, monkeypatch):
    """Регрессия на handlers/factcheck.py: форвард — самостоятельное действие,
    а не ответ на вопрос текущего экрана, и должен перехватываться раньше
    состояний FSM. Без этого форвард поста посреди диалога с тренером ушёл бы
    в ai_trainer.ai_question — «вопросом» с чужим текстом внутри, а не разбором."""
    import ai_trainer

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    origin = MessageOriginChannel(
        type="channel", date=dt.datetime.now(),
        chat=Chat(id=-100, type="channel"), message_id=1,
    )
    fsm = _fsm(dispatcher)
    await fsm.set_state(AITrainerFlow.chatting)
    try:
        winners = await _feed(
            dispatcher, "х" * 50, forward_origin=origin,
        )
        assert winners == ["handlers.factcheck.factcheck_forward"]
    finally:
        await fsm.clear()


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
