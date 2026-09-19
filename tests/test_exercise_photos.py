"""Своё фото упражнения: хранение файлом, ручки `/v1` и перенос из Telegram.

Приложение собирается тем же способом, что и в tests/test_api_v1_media.py —
отдельным Starlette поверх api_v1_media.routes: остальные маршруты `/v1`
этим тестам не нужны, а токен выпускается прямо через db, минуя `/auth/link`.

Каталог фото на время теста подменяется во временный (config.EXERCISE_PHOTO_DIR):
боевой путь — /data, писать в него из тестов нельзя, да и не нужно.
"""

import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.applications import Starlette

import api_v1_common as common
import api_v1_media
import config
import db as db_module
import exercise_photos

ApiError = common.ApiError

# Однопиксельный PNG — самый короткий honest-файл, который можно прогнать
# через весь путь «загрузили → отдали»; содержимое проверяется побайтово.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def photo_dir(tmp_path, monkeypatch):
    path = tmp_path / "exercise_photos"
    monkeypatch.setattr(config, "EXERCISE_PHOTO_DIR", str(path))
    return path


def _build_app() -> Starlette:
    return Starlette(
        routes=api_v1_media.routes,
        exception_handlers={
            ApiError: common.api_error_handler,
            Exception: common.unhandled_error_handler,
        },
    )


async def _linked_client(fresh_db, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    token = await fresh_db.issue_api_token(telegram_id)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_build_app()), base_url="http://test")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ---------- загрузка, отдача, удаление ----------


@pytest.mark.asyncio
async def test_upload_then_serve_returns_the_very_same_bytes(fresh_db, photo_dir):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)

    resp = await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)})
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"has_photo": True}

    ex = await fresh_db.get_exercise(ex_id)
    assert ex["custom_photo_path"]
    # Файл лежит у нас, а не «где-то в Telegram» — ради этого всё и затевалось.
    assert os.path.isfile(os.path.join(str(photo_dir), ex["custom_photo_path"]))

    got = await client.get(f"/exercises/{ex_id}/photo")
    assert got.status_code == 200
    assert got.content == PNG_BYTES
    assert got.headers["content-type"] == "image/png"
    # Пользовательское фото может смениться — вечного кэша ему нельзя.
    assert "immutable" not in got.headers["cache-control"]
    assert "must-revalidate" in got.headers["cache-control"]


@pytest.mark.asyncio
async def test_photo_404_until_something_is_uploaded(fresh_db):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)

    resp = await client.get(f"/exercises/{ex_id}/photo")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


@pytest.mark.asyncio
async def test_replacing_a_photo_drops_the_previous_file(fresh_db, photo_dir):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)

    await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)})
    first = (await fresh_db.get_exercise(ex_id))["custom_photo_path"]
    await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)})
    second = (await fresh_db.get_exercise(ex_id))["custom_photo_path"]

    assert first != second, "имя обязано смениться, иначе кэш клиента покажет старое фото"
    assert not os.path.exists(os.path.join(str(photo_dir), first))
    assert os.path.isfile(os.path.join(str(photo_dir), second))


@pytest.mark.asyncio
async def test_upload_from_app_clears_the_stale_telegram_link(fresh_db):
    """Ссылка на прежнюю картинку не должна пережить замену фото: иначе бот
    показывал бы в чате одно, а приложение — другое."""
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    await fresh_db.set_exercise_photo(ex_id, "OLD_FILE_ID")

    await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)})

    ex = await fresh_db.get_exercise(ex_id)
    assert ex["custom_photo_file_id"] is None
    assert ex["custom_photo_path"]


@pytest.mark.asyncio
async def test_delete_removes_row_and_file(fresh_db, photo_dir):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    await client.post(f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)})
    name = (await fresh_db.get_exercise(ex_id))["custom_photo_path"]

    resp = await client.delete(f"/exercises/{ex_id}/photo")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    ex = await fresh_db.get_exercise(ex_id)
    assert ex["custom_photo_path"] is None
    assert ex["custom_photo_file_id"] is None
    assert not os.path.exists(os.path.join(str(photo_dir), name))
    assert (await client.delete(f"/exercises/{ex_id}/photo")).status_code == 404


# ---------- чужое, слишком тяжёлое, не то ----------


@pytest.mark.asyncio
async def test_someone_elses_exercise_is_404_on_every_verb(fresh_db):
    other_id = await fresh_db.create_exercise(222, "Присед со штангой", group_id=None)
    await fresh_db.set_exercise_photo(other_id, None, "not_mine.jpg")
    client = await _linked_client(fresh_db, telegram_id=111)

    assert (await client.get(f"/exercises/{other_id}/photo")).status_code == 404
    upload = await client.post(
        f"/exercises/{other_id}/photo", json={"image_data_url": _data_url(PNG_BYTES)}
    )
    assert upload.status_code == 404
    assert upload.json()["error"] == "not_found"
    assert (await client.delete(f"/exercises/{other_id}/photo")).status_code == 404
    # Ничего не перезаписали — чужая строка осталась как была.
    assert (await fresh_db.get_exercise(other_id))["custom_photo_path"] == "not_mine.jpg"


@pytest.mark.asyncio
async def test_too_heavy_photo_is_rejected(fresh_db, photo_dir):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    heavy = b"\x00" * (api_v1_media.MAX_IMAGE_BYTES + 1)

    resp = await client.post(
        f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(heavy, "image/jpeg")}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "photo_too_big"
    assert (await fresh_db.get_exercise(ex_id))["custom_photo_path"] is None
    # Каталог даже не завели: отказ случился до записи файла.
    assert not os.path.isdir(str(photo_dir)) or os.listdir(str(photo_dir)) == []


@pytest.mark.asyncio
async def test_unsupported_format_is_415(fresh_db):
    client = await _linked_client(fresh_db)
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)

    resp = await client.post(
        f"/exercises/{ex_id}/photo", json={"image_data_url": _data_url(PNG_BYTES, "image/gif")}
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_photo_requires_auth(fresh_db):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_build_app()), base_url="http://test")
    assert (await client.get("/exercises/1/photo")).status_code == 401


# ---------- фото живёт вместе с упражнением ----------


@pytest.mark.asyncio
async def test_merge_carries_the_photo_over(fresh_db, photo_dir):
    keep_id = await fresh_db.create_exercise(111, "Жим лёжа", group_id=None)
    drop_id = await fresh_db.create_exercise(111, "жим штанги лёжа", group_id=None)
    name = exercise_photos.save(drop_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(drop_id, "FILE_ID", name)

    assert await fresh_db.merge_exercises(111, keep_id, drop_id) == fresh_db.MERGE_OK

    keep = await fresh_db.get_exercise(keep_id)
    assert keep["custom_photo_path"] == name
    assert keep["custom_photo_file_id"] == "FILE_ID"
    assert os.path.isfile(os.path.join(str(photo_dir), name))


@pytest.mark.asyncio
async def test_merge_drops_the_losing_photo_file(fresh_db, photo_dir):
    keep_id = await fresh_db.create_exercise(111, "Жим лёжа", group_id=None)
    drop_id = await fresh_db.create_exercise(111, "жим штанги лёжа", group_id=None)
    kept_name = exercise_photos.save(keep_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(keep_id, None, kept_name)
    dropped_name = exercise_photos.save(drop_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(drop_id, None, dropped_name)

    await fresh_db.merge_exercises(111, keep_id, drop_id)

    assert (await fresh_db.get_exercise(keep_id))["custom_photo_path"] == kept_name
    assert os.path.isfile(os.path.join(str(photo_dir), kept_name))
    assert not os.path.exists(os.path.join(str(photo_dir), dropped_name))


@pytest.mark.asyncio
async def test_deleting_an_unused_exercise_takes_its_file_along(fresh_db, photo_dir):
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    name = exercise_photos.save(ex_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(ex_id, None, name)

    assert await fresh_db.delete_exercise_if_unused(ex_id, 111) is True
    assert not os.path.exists(os.path.join(str(photo_dir), name))


# ---------- перенос из Telegram ----------


def _bot_downloading(mapping: dict[str, bytes | Exception]) -> MagicMock:
    """Подставной бот: download(file_id) отдаёт байты или падает — так и
    проверяется, что сбой одного файла не уносит остальные."""
    async def _download(file_id):
        outcome = mapping[file_id]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(read=lambda: outcome)

    bot = MagicMock()
    bot.download = AsyncMock(side_effect=_download)
    return bot


@pytest.mark.asyncio
async def test_backfill_pulls_photos_from_telegram(fresh_db, photo_dir):
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    await fresh_db.set_exercise_photo(ex_id, "FILE_ID")

    migrated = await exercise_photos.backfill_from_telegram(_bot_downloading({"FILE_ID": PNG_BYTES}))

    assert migrated == 1
    ex = await fresh_db.get_exercise(ex_id)
    assert ex["custom_photo_path"]
    # Ссылка остаётся: по ней бот переотправляет фото даром.
    assert ex["custom_photo_file_id"] == "FILE_ID"
    with open(os.path.join(str(photo_dir), ex["custom_photo_path"]), "rb") as fh:
        assert fh.read() == PNG_BYTES


@pytest.mark.asyncio
async def test_backfill_survives_a_broken_file_and_keeps_its_link(fresh_db):
    broken_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    good_id = await fresh_db.create_exercise(111, "Жим лёжа", group_id=None)
    await fresh_db.set_exercise_photo(broken_id, "BROKEN")
    await fresh_db.set_exercise_photo(good_id, "GOOD")

    migrated = await exercise_photos.backfill_from_telegram(
        _bot_downloading({"BROKEN": RuntimeError("file is gone"), "GOOD": PNG_BYTES})
    )

    assert migrated == 1
    broken = await fresh_db.get_exercise(broken_id)
    # Главное: упавшее скачивание не стёрло единственное, что было у этого
    # фото, — ссылку в Telegram.
    assert broken["custom_photo_file_id"] == "BROKEN"
    assert broken["custom_photo_path"] is None
    assert (await fresh_db.get_exercise(good_id))["custom_photo_path"]


@pytest.mark.asyncio
async def test_backfill_is_idempotent(fresh_db):
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    await fresh_db.set_exercise_photo(ex_id, "FILE_ID")
    bot = _bot_downloading({"FILE_ID": PNG_BYTES})

    assert await exercise_photos.backfill_from_telegram(bot) == 1
    assert await exercise_photos.backfill_from_telegram(bot) == 0
    assert bot.download.await_count == 1


@pytest.mark.asyncio
async def test_traversal_name_in_the_column_resolves_to_nothing(fresh_db):
    """Имя файла приезжает из базы, но выход за каталог всё равно не
    открывается: последний рубеж — сам exercise_photos.path_for."""
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    await fresh_db.set_exercise_photo(ex_id, None, "../../etc/passwd")
    client = await _linked_client(fresh_db)

    assert (await client.get(f"/exercises/{ex_id}/photo")).status_code == 404
    assert exercise_photos.path_for("../../etc/passwd") is None


@pytest.mark.asyncio
async def test_wiping_an_account_takes_its_photo_files(fresh_db, photo_dir):
    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    name = exercise_photos.save(ex_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(ex_id, None, name)

    await db_module.wipe_user_account(111)

    assert not os.path.exists(os.path.join(str(photo_dir), name))


# ---------- бот и приложение смотрят на одно и то же фото ----------


@pytest.mark.asyncio
async def test_bot_upload_stores_the_file_next_to_the_link(fresh_db, photo_dir):
    """Фото, присланное в Telegram, обязано лечь и на диск: иначе в
    приложении его не видно, а смена токена бота уносит его целиком."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

    import handlers.exercises as exercises_handler

    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=111, user_id=111)
    )
    await state.update_data(exm_exercise_id=ex_id)

    message = MagicMock()
    message.from_user = SimpleNamespace(id=111, language_code=None)
    message.chat = SimpleNamespace(id=111)
    message.photo = [SimpleNamespace(file_id="FILE_ID_FROM_TG")]
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    message.answer_photo = AsyncMock(return_value=SimpleNamespace(message_id=2, photo=None))
    message.bot = _bot_downloading({"FILE_ID_FROM_TG": PNG_BYTES})
    message.bot.delete_message = AsyncMock()

    await exercises_handler.exm_photo_entered(message, state)

    ex = await fresh_db.get_exercise(ex_id)
    assert ex["custom_photo_file_id"] == "FILE_ID_FROM_TG"
    assert ex["custom_photo_path"]
    with open(os.path.join(str(photo_dir), ex["custom_photo_path"]), "rb") as fh:
        assert fh.read() == PNG_BYTES


@pytest.mark.asyncio
async def test_bot_sends_the_file_when_the_link_is_gone_and_remembers_the_new_one(fresh_db):
    """Фото из приложения (file_id пустой) бот отправляет файлом ровно один
    раз, а дальше — снова дешёвой ссылкой."""
    from aiogram.types import FSInputFile

    ex_id = await fresh_db.create_exercise(111, "Присед со штангой", group_id=None)
    name = exercise_photos.save(ex_id, PNG_BYTES, "png")
    await fresh_db.set_exercise_photo(ex_id, None, name)
    ex = await fresh_db.get_exercise(ex_id)

    payload = exercise_photos.telegram_input(ex)
    assert isinstance(payload, FSInputFile)

    sent = SimpleNamespace(photo=[SimpleNamespace(file_id="FRESH_FILE_ID")])
    await exercise_photos.remember_sent_file_id(ex, sent)

    updated = await fresh_db.get_exercise(ex_id)
    assert updated["custom_photo_file_id"] == "FRESH_FILE_ID"
    assert exercise_photos.telegram_input(updated) == "FRESH_FILE_ID"
