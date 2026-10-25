"""Файл состояния диалога на сетевом томе — сбои записи.

В JSON-файле живёт незакрытая тренировка (открытые блоки, активное
упражнение). Обе ветки обработки сбоев записи не исполнялись: повтор после
«stale file handle» (errno 116 после перемонтирования тома) и запись, которая
не удалась совсем, — тогда обработчик не должен падать (кнопка осталась бы
без ответа на каждом апдейте), а состояние в памяти должно остаться и уехать
на диск первой же удачной записью.
"""

import errno
import json
import os

import pytest
from aiogram.fsm.storage.base import StorageKey

import fsm_storage
from fsm import WorkoutFlow

pytestmark = pytest.mark.asyncio

KEY = StorageKey(bot_id=1, chat_id=7, user_id=7)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(fsm_storage.time, "sleep", lambda _s: None)


def _flaky_replace(monkeypatch, failures: int):
    real_replace = os.replace
    left = {"n": failures}

    def replace(src, dst):
        if left["n"] > 0:
            left["n"] -= 1
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_replace(src, dst)

    monkeypatch.setattr(fsm_storage.os, "replace", replace)
    return left


async def test_stale_handle_is_retried_and_the_write_lands(tmp_path, monkeypatch):
    path = tmp_path / "fsm.json"
    storage = fsm_storage.JSONFileStorage(str(path))
    _flaky_replace(monkeypatch, failures=2)

    await storage.set_data(KEY, {"workout_id": 42, "open_blocks": {5: 9}})

    on_disk = json.loads(path.read_text())
    assert list(on_disk.values())[0]["data"]["workout_id"] == 42


async def test_persistent_failure_keeps_memory_and_recovers_on_next_write(tmp_path, monkeypatch):
    path = tmp_path / "fsm.json"
    storage = fsm_storage.JSONFileStorage(str(path))
    left = _flaky_replace(monkeypatch, failures=10**6)

    # Ни set_state, ни set_data не бросают — обработчик продолжает работать.
    await storage.set_state(KEY, WorkoutFlow.logging_set)
    await storage.set_data(KEY, {"workout_id": 42, "open_blocks": {5: 9}})
    assert await storage.get_state(KEY) == WorkoutFlow.logging_set.state
    assert (await storage.get_data(KEY))["open_blocks"] == {5: 9}
    assert not path.exists()

    # Том ожил: следующая запись уносит на диск всё накопленное.
    left["n"] = 0
    await storage.set_data(KEY, {"workout_id": 42, "open_blocks": {5: 9}, "active_exercise_id": 5})

    restarted = fsm_storage.JSONFileStorage(str(path))
    assert await restarted.get_state(KEY) == WorkoutFlow.logging_set.state
    data = await restarted.get_data(KEY)
    assert data["open_blocks"] == {5: 9}  # int-ключи пережили перезапуск
    assert data["active_exercise_id"] == 5


async def test_unreadable_quarantine_does_not_block_startup(tmp_path, monkeypatch):
    """Битый файл, который ещё и не переименовать (том только для чтения), —
    бот всё равно стартует с пустым состоянием."""
    path = tmp_path / "fsm.json"
    path.write_text("{not json")

    def deny(*_args):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(fsm_storage.os, "replace", deny)
    storage = fsm_storage.JSONFileStorage(str(path))

    assert await storage.get_data(KEY) == {}


async def test_drop_user_survives_unparseable_keys_in_the_file(tmp_path):
    """drop_user зовётся из сноса аккаунта ПОСЛЕ стирания базы: упади он на
    чужой битой строке файла — DELETE /v1/account ответил бы 500 на уже
    стёртый аккаунт, а состояние диалога осталось бы. user_keys такие строки
    уже пропускал, drop_user — нет (починено здесь же)."""
    path = tmp_path / "fsm.json"
    good = fsm_storage._key_to_str(KEY)
    path.write_text(json.dumps({"not a key": {"state": "x"}, "5": {"state": "z"}, good: {"state": "y"}}))
    storage = fsm_storage.JSONFileStorage(str(path))

    assert storage.user_keys(7) == [KEY]
    assert await storage.drop_user(7) == 1
    assert storage.user_keys(7) == []
    assert await storage.get_state(KEY) is None
