"""handlers.ai_trainer: приём видео подхода — лимиты и порядок проверок.

Порядок тут не косметика: длину Telegram сообщает в самом апдейте, а скачивание
и разбор стоят и трафика, и денег. Поэтому дорогое не должно случаться после
отказа — на это и смотрят тесты.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import ai_limits
import config
import db
from handlers import ai_trainer as handler

pytestmark = pytest.mark.asyncio


def _message(duration=10, file_size=1024, caption=None, user_id=777):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.caption = caption
    message.video = SimpleNamespace(
        duration=duration, file_size=file_size, mime_type="video/mp4", file_id="vid-1"
    )
    message.video_note = None
    message.animation = None
    message.reply = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
    message.bot = MagicMock()
    message.bot.download = AsyncMock(return_value=SimpleNamespace(read=lambda: b"bytes"))
    return message


@pytest.fixture(autouse=True)
def _cheap_day(monkeypatch):
    """По умолчанию сутки дешёвые: потолок по деньгам проверяется отдельным тестом."""
    monkeypatch.setattr(ai_limits, "daily_spend_usd", AsyncMock(return_value=0.0))


def _state(data=None):
    """FSM-двойник: между вопросом «что за упражнение» и ответом ролик живёт тут."""
    store = dict(data or {})
    st = MagicMock()
    st.get_data = AsyncMock(side_effect=lambda: dict(store))
    st.update_data = AsyncMock(side_effect=lambda **kw: store.update(kw))
    st.store = store
    return st


async def _send_and_name(message, choice="aivid:skip"):
    """Полный путь ролика без подписи: сначала вопрос, потом выбранное название.

    Разбор запускается только после ответа — раньше он шёл сразу, и на неверно
    угаданном упражнении полторы минуты и три цента уходили впустую.
    """
    state = _state()
    await handler.ai_video_question(message, state)
    callback = MagicMock()
    callback.from_user = message.from_user
    callback.data = choice
    callback.answer = AsyncMock()
    callback.message = message
    callback.message.edit_reply_markup = AsyncMock()
    await handler.ai_video_exercise_chosen(callback, state)
    return state


@pytest.fixture(autouse=True)
def _clear_busy():
    handler._busy.clear()
    yield
    handler._busy.clear()


@pytest.fixture
def wired(monkeypatch):
    """Всё подключено, квота свободна, разбор возвращает пустой, но валидный ответ."""
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")
    monkeypatch.setattr(db, "get_ai_video_count_today", AsyncMock(return_value=0))
    monkeypatch.setattr(db, "increment_ai_video_count", AsyncMock())
    analyze = AsyncMock(return_value={
        "exercise": "присед", "reps_seen": 3,
        "view": {"angle": "сбоку", "usable": True, "problem": ""},
        "description": [], "observations": [], "not_visible": [], "camera_advice": "",
    })
    monkeypatch.setattr(handler.video_analysis, "analyze", analyze)
    monkeypatch.setattr(
        db, "list_user_exercises",
        AsyncMock(return_value=[{"display_name": "Присед со штангой"}]),
    )
    monkeypatch.setattr(db, "count_user_exercises", AsyncMock(return_value=1))
    handle = AsyncMock()
    monkeypatch.setattr(handler, "_handle_question", handle)
    return SimpleNamespace(analyze=analyze, handle=handle)


async def test_video_analyzed_and_handed_to_trainer(wired):
    message = _message()
    await _send_and_name(message)

    wired.analyze.assert_awaited_once()
    # Тренер получает наблюдения текстом, а не само видео.
    kwargs = wired.handle.await_args.kwargs
    assert "присед" in kwargs["video_context"]
    assert kwargs["history_question"] == "[прислал видео подхода]"
    db.increment_ai_video_count.assert_awaited_once()


async def test_caption_becomes_the_question(wired):
    """Подпись — вопрос человека, и он доезжает до тренера даже когда названия
    упражнения в ней нет и пришлось спросить отдельно."""
    message = _message(caption="почему поясницу тянет")
    await _send_and_name(message, choice="aivid:ex:0")

    args = wired.handle.await_args.args
    assert args[2] == "почему поясницу тянет"
    assert wired.handle.await_args.kwargs["history_question"] == "[видео] почему поясницу тянет"


async def test_too_long_video_rejected_before_download(wired):
    message = _message(duration=config.MAX_VIDEO_SECONDS + 1)
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    assert str(config.MAX_VIDEO_SECONDS) in message.reply.await_args.args[0]


async def test_daily_limit_blocks_before_download(monkeypatch, wired):
    monkeypatch.setattr(
        db, "get_ai_video_count_today", AsyncMock(return_value=config.AI_VIDEO_DAILY_LIMIT)
    )
    monkeypatch.setattr(handler, "ai_keyboard", AsyncMock(return_value=None))
    message = _message()
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    assert str(config.AI_VIDEO_DAILY_LIMIT) in message.reply.await_args.args[0]


async def test_preview_block_still_analyzes_video(monkeypatch, wired):
    """Регрессия: у своего аккаунта первое превью-предупреждение за сутки
    раньше проглатывало сам ролик — «Понятно» лечило только следующую попытку,
    а этот вопрос надо было слать заново. Разбор должен идти сразу же."""
    monkeypatch.setattr(
        handler.ai_limits, "check",
        AsyncMock(return_value=handler.ai_limits.Block(
            kind="video", log="video preview", user_text="лимит рядом", preview=True,
        )),
    )
    monkeypatch.setattr(handler, "ai_keyboard", AsyncMock(return_value=None))
    message = _message()
    await _send_and_name(message)

    wired.analyze.assert_awaited_once()
    wired.handle.assert_awaited_once()
    db.increment_ai_video_count.assert_awaited_once()
    message.reply.assert_awaited()


async def test_daily_cost_cap_turns_video_off_before_download(monkeypatch, wired):
    """Дорогие сутки выключают разбор видео у всех сразу — даже у того, кто
    сегодня не прислал ни одного ролика."""
    monkeypatch.setattr(
        ai_limits, "daily_spend_usd",
        AsyncMock(return_value=config.AI_DAILY_COST_SOFT_CAP_USD + 1),
    )
    monkeypatch.setattr(handler, "ai_keyboard", AsyncMock(return_value=None))
    message = _message()
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    assert "завтра" in message.reply.await_args.args[0]


async def test_oversized_file_rejected_before_download(wired):
    message = _message(file_size=config.MAX_VIDEO_BYTES + 1)
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()


async def test_missing_key_tells_user_to_type(monkeypatch, wired):
    monkeypatch.setattr(config, "NOVITA_API_KEY", "")
    message = _message()
    await handler.ai_video_question(message, _state())

    wired.analyze.assert_not_awaited()
    assert "текстом" in message.reply.await_args.args[0]


async def test_failed_analysis_does_not_spend_quota(monkeypatch, wired):
    """Поломка провайдера не должна стоить человеку одного из десяти разборов."""
    monkeypatch.setattr(handler.video_analysis, "analyze", AsyncMock(return_value=None))
    message = _message()
    await _send_and_name(message)

    db.increment_ai_video_count.assert_not_awaited()
    wired.handle.assert_not_awaited()
    assert "Не смог разобрать" in message.reply.await_args.args[0]


async def test_second_video_while_busy_is_refused(wired):
    handler._busy.add(777)
    message = _message()
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()


async def test_busy_released_after_run(wired):
    message = _message()
    await handler.ai_video_question(message, _state())
    assert 777 not in handler._busy


async def test_video_note_accepted(wired):
    """Кружок — тот же путь: у него есть и duration, и file_size."""
    message = _message()
    message.video = None
    message.video_note = SimpleNamespace(duration=8, file_size=2048, file_id="note-1")
    await _send_and_name(message)

    wired.analyze.assert_awaited_once()
    # У кружка своего mime_type нет — подставляем mp4, а не падаем на getattr.
    assert wired.analyze.await_args.kwargs["mime_type"] == "video/mp4"


async def test_animation_accepted(wired):
    """Ролик без аудиодорожки Telegram присылает как animation (в клиенте «GIF»).

    Именно на этом фича молча не работала: фильтр ловил только video, и снятое
    молча видео из зала улетало в общий fallback «Не понял».
    """
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(duration=12, file_size=3072, mime_type="video/mp4", file_id="anim-1")
    await _send_and_name(message)

    wired.analyze.assert_awaited_once()


async def test_animation_mime_type_passed_through(wired):
    """Настоящий .gif приходит как image/gif — врать про тип в data: URL нельзя."""
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(duration=5, file_size=1024, mime_type="image/gif", file_id="gif-1")
    await _send_and_name(message)

    assert wired.analyze.await_args.kwargs["mime_type"] == "image/gif"


async def test_animation_length_limit_applies(wired):
    """Лимит длины не должен обходиться через тип: GIF считается так же."""
    message = _message()
    message.video = None
    message.animation = SimpleNamespace(
        duration=config.MAX_VIDEO_SECONDS + 5, file_size=1024, mime_type="video/mp4",
        file_id="long-1",
    )
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()


# ---------- «что за упражнение» спрашивается ДО разбора ----------


async def test_video_without_caption_asks_before_spending_anything(wired):
    """Раньше модель угадывала упражнение сама, и на неуверенной догадке тренер
    переспрашивал — но ролик к тому моменту был уже посмотрен и оплачен, а
    наблюдения собраны под чужое движение и переиспользовать их нельзя.

    Полторы минуты и три цента впустую, плюс списанный вопрос из дневной квоты
    за то, что бот не разобрался. Вопрос кнопками не стоит ни одного вызова.
    """
    message = _message()
    await handler.ai_video_question(message, _state())

    message.bot.download.assert_not_awaited()
    wired.analyze.assert_not_awaited()
    wired.handle.assert_not_awaited()
    db.increment_ai_video_count.assert_not_awaited()
    assert "Что за упражнение" in message.reply.await_args.args[0]


async def test_the_question_offers_the_athletes_own_exercises(wired):
    message = _message()
    await handler.ai_video_question(message, _state())

    markup = message.reply.await_args.kwargs["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Присед со штангой" in labels
    # И всегда есть выход для того, чего в списке нет.
    assert any("без названия" in label for label in labels)


async def test_caption_naming_a_known_exercise_skips_the_question(monkeypatch, wired):
    """Назвал упражнение в подписи — спрашивать нечего."""
    monkeypatch.setattr(
        db, "list_user_exercises",
        AsyncMock(return_value=[{"display_name": "Румынская тяга"}]),
    )
    message = _message(caption="румынская тяга")
    await handler.ai_video_question(message, _state())

    wired.analyze.assert_awaited_once()
    # В разбор уезжает название из каталога, а не то, как его набрали.
    assert wired.analyze.await_args.kwargs["exercise_hint"] == "Румынская тяга"
    message.reply.assert_not_awaited()


async def test_caption_that_is_not_an_exercise_still_asks(wired):
    """Живой провал: «оцени технику» засчитывали ЗА НАЗВАНИЕ и уходили в разбор
    мимо вопроса. Модель не находила там движения, угадывала становую и
    возвращала среднюю уверенность — то есть ровно тот дорогой путь, ради
    отмены которого вопрос и заводили."""
    message = _message(caption="оцени технику")
    await handler.ai_video_question(message, _state())

    wired.analyze.assert_not_awaited()
    message.bot.download.assert_not_awaited()
    assert "Что за упражнение" in message.reply.await_args.args[0]


async def test_exercise_named_inside_a_longer_caption_counts(monkeypatch, wired):
    monkeypatch.setattr(
        db, "list_user_exercises",
        AsyncMock(return_value=[{"display_name": "Жим лёжа"}]),
    )
    message = _message(caption="вот жим лёжа, глянь локти")
    await handler.ai_video_question(message, _state())

    assert wired.analyze.await_args.kwargs["exercise_hint"] == "Жим лёжа"


async def test_longest_catalog_match_wins(monkeypatch, wired):
    """«Жим лёжа узким хватом» точнее, чем «Жим лёжа», а короткое совпадает всегда."""
    monkeypatch.setattr(
        db, "list_user_exercises",
        AsyncMock(return_value=[
            {"display_name": "Жим лёжа"},
            {"display_name": "Жим лёжа узким хватом"},
        ]),
    )
    message = _message(caption="жим лёжа узким хватом")
    await handler.ai_video_question(message, _state())

    assert wired.analyze.await_args.kwargs["exercise_hint"] == "Жим лёжа узким хватом"


async def test_chosen_exercise_reaches_the_analysis(wired):
    message = _message()
    await _send_and_name(message, choice="aivid:ex:0")

    assert wired.analyze.await_args.kwargs["exercise_hint"] == "Присед со штангой"


async def test_skip_analyses_without_a_name(wired):
    """«Разбери так» — законный выход: модель определит сама и скажет, насколько уверена."""
    message = _message()
    await _send_and_name(message, choice="aivid:skip")

    assert wired.analyze.await_args.kwargs["exercise_hint"] is None


async def test_stale_button_after_restart_does_not_crash(wired):
    """FSM переживает рестарт, а нажатие может прийти когда ролика уже нет."""
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=777)
    callback.data = "aivid:skip"
    callback.answer = AsyncMock()
    callback.message = _message()
    await handler.ai_video_exercise_chosen(callback, _state())

    wired.analyze.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get("show_alert") is True


async def test_busy_released_after_the_button_path(wired):
    message = _message()
    await _send_and_name(message)
    assert 777 not in handler._busy


# ---------- название текстом и листалка по каталогу ----------


async def test_typed_answer_becomes_the_exercise_name(wired):
    """Пока висит непрослушанный ролик, текст — это ответ про упражнение, а не
    новый вопрос тренеру. Перехват внутри хендлера по состоянию, тем же приёмом,
    что и опросник."""
    message = _message()
    state = _state()
    await handler.ai_video_question(message, state)

    answer = _message()
    answer.text = "румынская тяга"
    await handler.ai_question(answer, state)

    assert wired.analyze.await_args.kwargs["exercise_hint"] == "румынская тяга"


async def test_pending_video_is_cleared_so_the_next_text_is_a_normal_question(wired):
    """Иначе следующий вопрос тренеру молча уехал бы как название упражнения."""
    message = _message()
    state = _state()
    await handler.ai_video_question(message, state)

    answer = _message()
    answer.text = "жим лёжа"
    await handler.ai_question(answer, state)

    assert (await state.get_data()).get("aivid_pending") is None


async def test_catalog_can_be_paged_without_touching_the_video(wired, monkeypatch):
    """Листание — это ещё вопрос, а не ответ: ролик не качается, замок не занят."""
    monkeypatch.setattr(db, "count_user_exercises", AsyncMock(return_value=20))
    message = _message()
    state = _state()
    await handler.ai_video_question(message, state)

    callback = MagicMock()
    callback.from_user = message.from_user
    callback.data = "aivid:page:1"
    callback.answer = AsyncMock()
    callback.message = message
    callback.message.edit_reply_markup = AsyncMock()
    await handler.ai_video_exercise_chosen(callback, state)

    wired.analyze.assert_not_awaited()
    assert 777 not in handler._busy
    assert (await state.get_data())["aivid_pending"]["page"] == 1
    assert db.list_user_exercises.await_args.kwargs["offset"] == handler.VIDEO_EXERCISE_CHOICES


async def test_pager_arrows_appear_only_where_there_is_somewhere_to_go(monkeypatch, wired):
    monkeypatch.setattr(db, "count_user_exercises", AsyncMock(return_value=20))
    message = _message()
    await handler.ai_video_question(message, _state())

    labels = [
        b.text for row in message.reply.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("ещё" in label for label in labels)
    # С первой страницы назад некуда.
    assert not any("назад" in label for label in labels)


async def test_long_exercise_name_survives_the_64_byte_callback_limit(monkeypatch, wired):
    """Кириллица в callback_data не помещается — имена живут в FSM, в кнопке индекс."""
    long_name = "Разгибания на трицепс в кроссовере с канатной рукоятью"
    monkeypatch.setattr(
        db, "list_user_exercises", AsyncMock(return_value=[{"display_name": long_name}])
    )
    message = _message()
    state = _state()
    await handler.ai_video_question(message, state)

    markup = message.reply.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert all(len(d.encode()) <= 64 for d in data)

    callback = MagicMock()
    callback.from_user = message.from_user
    callback.data = "aivid:ex:0"
    callback.answer = AsyncMock()
    callback.message = message
    callback.message.edit_reply_markup = AsyncMock()
    await handler.ai_video_exercise_chosen(callback, state)

    assert wired.analyze.await_args.kwargs["exercise_hint"] == long_name
