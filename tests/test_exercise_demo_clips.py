"""Видео-демонстрация упражнения: клип вытесняет пару фото там, где он снят,
и нигде не ломает экраны там, где его ещё нет.

Клипы приезжают по одному (scripts/gen_exercise_demos.py), поэтому оба пути
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
    bot.send_animation = AsyncMock(
        return_value=SimpleNamespace(
            message_id=901, animation=SimpleNamespace(file_id="anim_file_id")
        )
    )
    bot.send_media_group = AsyncMock(
        return_value=[
            SimpleNamespace(message_id=902, photo=[SimpleNamespace(file_id="photo_id_1")]),
            SimpleNamespace(message_id=903, photo=[SimpleNamespace(file_id="photo_id_2")]),
        ]
    )
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=904))
    return bot


async def _state(user_id: int = 7) -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )
    await state.set_state(WorkoutFlow.logging_set)
    return state


# --- exercise_media ---------------------------------------------------------


def test_no_clip_for_unknown_exercise():
    assert exercise_media.get_animation("Совсем не упражнение") is None


def test_no_clip_when_file_is_not_on_disk_yet():
    """Упражнение с фото, но без снятого клипа — обычное состояние на раскатке."""
    assert exercise_media.get_animation(EX_WITH_PHOTO) is None


def test_clip_is_found_once_the_file_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(tmp_path))
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[EX_WITH_PHOTO]
    (tmp_path / f"{slug}_demo.mp4").write_bytes(b"mp4")
    assert exercise_media.get_animation(EX_WITH_PHOTO) == str(tmp_path / f"{slug}_demo.mp4")


def test_clip_survives_a_rename(tmp_path, monkeypatch):
    """Ключ — original_name: пользователь вправе переименовать упражнение, и
    клип не должен от этого пропадать (тот же разбор, что в catalog_key)."""
    monkeypatch.setattr(exercise_media, "MEDIA_DIR", str(tmp_path))
    slug = exercise_media.EXERCISE_IMAGE_SLUGS[EX_WITH_PHOTO]
    (tmp_path / f"{slug}_demo.mp4").write_bytes(b"mp4")
    renamed = dict(_exercise_row(), name="Тяга сверху, моё название")
    assert exercise_media.get_animation_for(renamed) is not None


# --- живой трекер -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sticky_sends_the_clip_instead_of_the_photo_pair(monkeypatch):
    monkeypatch.setattr(exercise_media, "get_animation_for", lambda ex: "/clips/demo.mp4")
    bot = _bot()
    ids = await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert ids == [901]
    bot.send_animation.assert_awaited_once()
    bot.send_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticky_remembers_the_clip_file_id(monkeypatch):
    """Второй показ того же упражнения не должен заново заливать файл —
    ровно та же экономия, ради которой кэш заведён для фото."""
    monkeypatch.setattr(exercise_media, "get_animation_for", lambda ex: "/clips/demo.mp4")
    bot = _bot()
    await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert exercise_media.cached_file_id("/clips/demo.mp4") == "anim_file_id"

    await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert bot.send_animation.await_args.kwargs["animation"] == "anim_file_id"


@pytest.mark.asyncio
async def test_sticky_falls_back_to_photos_without_a_clip(monkeypatch):
    monkeypatch.setattr(exercise_media, "get_animation_for", lambda ex: None)
    bot = _bot()
    ids = await workout._send_sticky_photo(bot, 42, _exercise_row())
    assert ids == [902, 903]
    bot.send_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_photo_still_wins_over_the_clip(monkeypatch):
    """Своё фото пользователя било каталожное и продолжает бить: клип — это
    улучшенный каталог, а не повод перебить то, что человек снял сам."""
    monkeypatch.setattr(exercise_media, "get_animation_for", lambda ex: "/clips/demo.mp4")
    bot = _bot()
    ex = dict(_exercise_row(), custom_photo_file_id="user_photo")
    ids = await workout._send_sticky_photo(bot, 42, ex)
    assert ids == [904]
    bot.send_animation.assert_not_awaited()


# --- карточка упражнения ----------------------------------------------------


@pytest.mark.asyncio
async def test_exercise_card_sends_the_clip(monkeypatch):
    from handlers import exercises

    monkeypatch.setattr(exercise_media, "get_animation_for", lambda ex: "/clips/demo.mp4")
    monkeypatch.setattr(exercises, "_exercise_group_name", AsyncMock(return_value="Спина"))
    message = MagicMock()
    message.answer_animation = AsyncMock(
        return_value=SimpleNamespace(
            message_id=905, animation=SimpleNamespace(file_id="anim_file_id")
        )
    )
    message.answer_media_group = AsyncMock()
    message.bot = MagicMock()
    state = await _state()

    assert await exercises._send_exercise_images(message, _exercise_row(), state) is True
    message.answer_animation.assert_awaited_once()
    message.answer_media_group.assert_not_awaited()
    assert (await state.get_data())["exm_media_msg_ids"] == [905]


# --- пайплайн генерации -----------------------------------------------------


def test_pilot_names_are_real_templates():
    """Опечатка в списке пилота стоила бы одного «нет такого упражнения»
    посреди платного прогона."""
    from scripts import gen_exercise_demos

    for exercise in gen_exercise_demos.PILOT:
        assert exercise in exercise_media.EXERCISE_IMAGE_SLUGS, exercise
