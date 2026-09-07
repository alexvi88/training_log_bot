"""Демонстрация движения одной картинкой: коллаж вытесняет пару фото там, где он
нарисован, и нигде не ломает экраны там, где его ещё нет.

Картинки приезжают по одной (scripts/gen_exercise_demos.py), поэтому оба пути
живут одновременно неопределённо долго — и проверять надо именно развилку, а
не «как стало».
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import exercise_media
from fsm import WorkoutFlow
from handlers import workout

EX_WITH_PHOTO = "Тяга верхнего блока"
COLLAGE = "/collages/demo.jpg"


@pytest.fixture(autouse=True)
def _clear_file_id_cache():
    exercise_media._FILE_IDS.clear()
    yield
    exercise_media._FILE_IDS.clear()


def _exercise_row(name: str = EX_WITH_PHOTO):
    return {
        "id": 1,
        "name": name,
        "original_name": name,
        "display_name": name,
        "custom_photo_file_id": None,
        "description": None,
        "equipment": None,
        "unilateral": 0,
        "attachment": None,
        "created_at": "2026-08-01T10:00:00",
        "primary_group_id": None,
    }


def _bot():
    bot = MagicMock()
    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(
            message_id=901, photo=[SimpleNamespace(file_id="collage_file_id")]
        )
    )
    bot.send_media_group = AsyncMock(
        return_value=[
            SimpleNamespace(message_id=902, photo=[SimpleNamespace(file_id="photo_id_1")]),
            SimpleNamespace(message_id=903, photo=[SimpleNamespace(file_id="photo_id_2")]),
        ]
    )
    return bot


async def _state(user_id: int = 7) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.logging_set)
    return state


# --- exercise_media ---------------------------------------------------------


def test_no_collage_for_unknown_exercise():
    assert exercise_media.get_collage("Совсем не упражнение") is None


def test_no_collage_when_file_is_not_on_disk_yet():
    """Упражнение с фото, но без нарисованного коллажа — обычное состояние
    на раскатке."""
    assert exercise_media.get_collage(EX_WITH_PHOTO) is None


def test_collage_is_found_once_the_file_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(tmp_path))
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[EX_WITH_PHOTO]
    (tmp_path / f"{slug}_demo.jpg").write_bytes(b"jpg")
    assert exercise_media.get_collage(EX_WITH_PHOTO) == str(tmp_path / f"{slug}_demo.jpg")


def test_collage_survives_a_rename(tmp_path, monkeypatch):
    """Ключ — original_name: пользователь вправе переименовать упражнение, и
    картинка не должна от этого пропадать (тот же разбор, что в catalog_key)."""
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(tmp_path))
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[EX_WITH_PHOTO]
    (tmp_path / f"{slug}_demo.jpg").write_bytes(b"jpg")
    renamed = dict(_exercise_row(), name="Тяга сверху, моё название")
    assert exercise_media.get_collage_for(renamed) is not None


# --- живой трекер -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sticky_sends_the_collage_instead_of_the_photo_pair(monkeypatch):
    monkeypatch.setattr(exercise_media, "get_collage_for", lambda ex: COLLAGE)
    bot = _bot()
    ids = await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert ids == [901]
    bot.send_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticky_remembers_the_collage_file_id(monkeypatch):
    """Второй показ того же упражнения не должен заново заливать файл — ровно
    та же экономия, ради которой кэш заведён для фото."""
    monkeypatch.setattr(exercise_media, "get_collage_for", lambda ex: COLLAGE)
    bot = _bot()
    await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert exercise_media.cached_file_id(COLLAGE) == "collage_file_id"

    await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert bot.send_photo.await_args.kwargs["photo"] == "collage_file_id"


@pytest.mark.asyncio
async def test_sticky_falls_back_to_photos_without_a_collage(monkeypatch):
    monkeypatch.setattr(exercise_media, "get_collage_for", lambda ex: None)
    bot = _bot()
    ids = await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert ids == [902, 903]
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_photo_still_wins_over_the_collage(monkeypatch):
    """Своё фото пользователя било каталожное и продолжает бить: коллаж — это
    улучшенный каталог, а не повод перебить то, что человек снял сам."""
    monkeypatch.setattr(exercise_media, "get_collage_for", lambda ex: COLLAGE)
    bot = _bot()
    ex = dict(_exercise_row(), custom_photo_file_id="user_photo")
    await workout._send_sticky_photo(bot, 42, ex)
    assert bot.send_photo.await_args.kwargs["photo"] == "user_photo"


# --- карточка упражнения ----------------------------------------------------


@pytest.mark.asyncio
async def test_exercise_card_sends_the_collage(monkeypatch):
    from handlers import exercises

    monkeypatch.setattr(exercise_media, "get_collage_for", lambda ex: COLLAGE)
    monkeypatch.setattr(exercises, "_exercise_group_name", AsyncMock(return_value="Спина"))
    message = MagicMock()
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(
            message_id=905, photo=[SimpleNamespace(file_id="collage_file_id")]
        )
    )
    message.answer_media_group = AsyncMock()
    message.bot = MagicMock()
    state = await _state()

    assert await exercises._send_exercise_images(message, _exercise_row(), state) is True
    message.answer_photo.assert_awaited_once()
    message.answer_media_group.assert_not_awaited()
    assert (await state.get_data())["exm_media_msg_ids"] == [905]


@pytest.mark.asyncio
async def test_template_preview_finds_the_collage_for_an_english_reader(tmp_path, monkeypatch):
    """Предпросмотр шаблона получает строку с ЛОКАЛИЗОВАННЫМ именем
    (_localized_template_row), а картинка ключуется идентичностью — русским
    original_name. Разъедется это молча и только у английских читателей,
    поэтому проверяем именно локализованную строку, а не каноническую.
    """
    from handlers import exercises

    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(tmp_path))
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[EX_WITH_PHOTO]
    (tmp_path / f"{slug}_demo.jpg").write_bytes(b"jpg")
    localized = dict(
        _exercise_row(), name="Lat pulldown", display_name="Lat pulldown", original_name=EX_WITH_PHOTO
    )
    message = MagicMock()
    message.answer_photo = AsyncMock(
        return_value=SimpleNamespace(
            message_id=906, photo=[SimpleNamespace(file_id="collage_file_id")]
        )
    )
    message.answer_media_group = AsyncMock()
    message.answer = AsyncMock()

    await exercises._send_template_preview(message, localized, "Lat pulldown", None, [])
    message.answer_photo.assert_awaited_once()
    message.answer.assert_not_awaited()


# --- пайплайн генерации -----------------------------------------------------


def test_pilot_names_are_real_templates():
    """Опечатка в списке пилота стоила бы одного «нет такого упражнения»
    посреди платного прогона."""
    from scripts import gen_exercise_demos

    for exercise in gen_exercise_demos.PILOT:
        assert exercise in exercise_media.EXERCISE_IMAGE_SLUGS, exercise


def test_sheet_request_carries_coach_then_start_then_end():
    """Промпт адресуется к картинкам по номерам: первая — тренер, вторая —
    старт движения, третья — конец. Порядок тут часть смысла, а не деталь
    сборки: перепутать старт с концом значит нарисовать повтор задом наперёд.

    И их именно три. Когда фото было одно, модель рисовала его во всех четырёх
    клетках — амплитуда выходила нулевой.
    """
    from scripts import gen_exercise_demos

    body, boundary = gen_exercise_demos._multipart(
        {"model": "gpt-image-1"},
        [
            ("image[]", gen_exercise_demos.COACH_REFERENCE),
            ("image[]", gen_exercise_demos.MEDIA_DIR / "barbell_squat_1.jpg"),
            ("image[]", gen_exercise_demos.MEDIA_DIR / "barbell_squat_2.jpg"),
        ],
    )
    assert body.count(b'name="image[]"') == 3
    assert (
        body.index(b"coach_incoming_call.jpg")
        < body.index(b"barbell_squat_1.jpg")
        < body.index(b"barbell_squat_2.jpg")
    )
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_coach_reference_image_is_where_the_pipeline_looks_for_it():
    """Эталон тренера уходит в генерацию картинкой. Переименуют файл — персонаж
    молча начнёт задаваться одними словами, и в карточках поедет второй тренер;
    словами это не ловится, файлом ловится."""
    from scripts import gen_exercise_demos

    assert gen_exercise_demos.COACH_REFERENCE.is_file(), gen_exercise_demos.COACH_REFERENCE
