"""JSONFileStorage: aiogram FSM storage persisted to a JSON file on disk."""

import json

import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from fsm_storage import JSONFileStorage

pytestmark = pytest.mark.asyncio


class _Flow(StatesGroup):
    waiting = State()


def _key(user_id: int = 1) -> StorageKey:
    return StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)


async def test_get_state_and_get_data_default_for_unknown_key(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    assert await storage.get_state(_key()) is None
    assert await storage.get_data(_key()) == {}


async def test_set_state_with_state_instance(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_state(_key(), _Flow.waiting)
    assert await storage.get_state(_key()) == "_Flow:waiting"


async def test_set_state_with_plain_string(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_state(_key(), "some_state")
    assert await storage.get_state(_key()) == "some_state"


async def test_set_state_none_clears_it(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_state(_key(), _Flow.waiting)
    await storage.set_state(_key(), None)
    assert await storage.get_state(_key()) is None


async def test_set_data_and_get_data_round_trip(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_data(_key(), {"foo": "bar", "n": 3})
    assert await storage.get_data(_key()) == {"foo": "bar", "n": 3}


async def test_get_data_returns_a_copy_not_a_live_reference(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_data(_key(), {"foo": "bar"})
    data = await storage.get_data(_key())
    data["foo"] = "mutated"
    assert await storage.get_data(_key()) == {"foo": "bar"}


async def test_state_and_data_are_isolated_per_key(tmp_path):
    storage = JSONFileStorage(str(tmp_path / "fsm.json"))
    await storage.set_state(_key(1), "state_a")
    await storage.set_data(_key(1), {"who": "a"})
    await storage.set_state(_key(2), "state_b")
    await storage.set_data(_key(2), {"who": "b"})

    assert await storage.get_state(_key(1)) == "state_a"
    assert await storage.get_data(_key(1)) == {"who": "a"}
    assert await storage.get_state(_key(2)) == "state_b"
    assert await storage.get_data(_key(2)) == {"who": "b"}


async def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "fsm.json")
    first = JSONFileStorage(path)
    await first.set_state(_key(), _Flow.waiting)
    await first.set_data(_key(), {"step": 2})

    second = JSONFileStorage(path)
    assert await second.get_state(_key()) == "_Flow:waiting"
    assert await second.get_data(_key()) == {"step": 2}


async def test_writes_are_flushed_immediately_to_disk(tmp_path):
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    await storage.set_data(_key(), {"x": 1})
    on_disk = json.loads(path.read_text())
    assert list(on_disk.values())[0]["data"] == {"x": 1}


async def test_close_creates_file_even_without_prior_writes(tmp_path):
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    assert not path.exists()
    await storage.close()
    assert path.exists()
    assert json.loads(path.read_text()) == {}


async def test_int_dict_keys_survive_a_restart(tmp_path):
    """open_blocks/last_by_exercise are keyed by int exercise_id; a JSON round-trip
    (e.g. process restart reloading the persisted file) must not turn those keys
    into strings, or lookups by int id in the handlers silently miss and break
    set logging.
    """
    path = str(tmp_path / "fsm.json")
    storage = JSONFileStorage(path)
    key = _key()
    await storage.set_data(key, {"open_blocks": {42: 99}, "active_exercise_id": 42})

    # Simulate a restart: a fresh storage instance reloading from disk.
    restarted = JSONFileStorage(path)
    data = await restarted.get_data(key)

    assert data["open_blocks"].get(data["active_exercise_id"]) == 99
