"""JSONFileStorage: aiogram FSM storage persisted to a JSON file on disk."""

import asyncio
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


# ---------- corrupt/empty state file on disk ----------


async def test_corrupt_json_does_not_crash_startup(tmp_path):
    """This constructor runs from Dispatcher(storage=...) in main() — an
    unguarded exception here used to take the whole bot down before it could
    serve a single update. A bad file (truncated write, full disk, a botched
    restore) is recoverable: FSM state is disposable, workout.py rebuilds an
    active workout straight from the DB (see _reopen_exercises)."""
    path = tmp_path / "fsm.json"
    path.write_text('{"garbage": "truncated mid-str')

    storage = JSONFileStorage(str(path))

    assert await storage.get_state(_key()) is None
    assert await storage.get_data(_key()) == {}


async def test_empty_file_does_not_crash_startup(tmp_path):
    """An empty file (0 bytes) is what a failed write can leave behind, and
    json.load rejects it just as hard as garbage content."""
    path = tmp_path / "fsm.json"
    path.write_text("")

    storage = JSONFileStorage(str(path))

    assert await storage.get_data(_key()) == {}


async def test_corrupt_file_is_quarantined_not_silently_lost(tmp_path):
    path = tmp_path / "fsm.json"
    path.write_text("not json at all")

    JSONFileStorage(str(path))

    assert not path.exists()  # moved aside, not left in place to fail again
    quarantined = list(tmp_path.glob("fsm.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "not json at all"


async def test_storage_works_normally_after_recovering_from_corruption(tmp_path):
    """Recovering from a bad file must leave a fully usable storage, not just
    one that avoids crashing."""
    path = tmp_path / "fsm.json"
    path.write_text("{broken")

    storage = JSONFileStorage(str(path))
    await storage.set_state(_key(), _Flow.waiting)
    await storage.set_data(_key(), {"workout_id": 5})

    assert await storage.get_state(_key()) == "_Flow:waiting"
    assert await storage.get_data(_key()) == {"workout_id": 5}


# ---------- pruning dead entries, so the file doesn't grow forever ----------


async def test_state_clear_sequence_prunes_the_entry(tmp_path):
    """aiogram's state.clear() calls set_state(None) then set_data({}) — the
    end of every flow (finishing a workout, backing out to the menu, ...).
    Without pruning, every user who ever interacted with the bot even once
    stays in the file forever."""
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    key = _key()
    await storage.set_data(key, {"workout_id": 5})
    await storage.set_state(key, _Flow.waiting)

    # The clear sequence:
    await storage.set_state(key, None)
    await storage.set_data(key, {})

    assert json.loads(path.read_text()) == {}


async def test_pruning_does_not_touch_other_keys(tmp_path):
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    active, finished = _key(1), _key(2)
    await storage.set_data(active, {"workout_id": 1})
    await storage.set_state(active, _Flow.waiting)
    await storage.set_data(finished, {"workout_id": 2})
    await storage.set_state(finished, _Flow.waiting)

    await storage.set_state(finished, None)
    await storage.set_data(finished, {})

    assert await storage.get_data(active) == {"workout_id": 1}
    assert await storage.get_data(finished) == {}
    on_disk = json.loads(path.read_text())
    assert len(on_disk) == 1  # only the active one survives


async def test_state_alone_without_data_is_not_pruned(tmp_path):
    """A state with no data yet (e.g. mid-flow before the first set_data call)
    must not be mistaken for a finished session."""
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    key = _key()
    await storage.set_state(key, _Flow.waiting)

    assert await storage.get_state(key) == "_Flow:waiting"
    assert len(json.loads(path.read_text())) == 1


async def test_data_alone_without_state_is_not_pruned(tmp_path):
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    key = _key()
    await storage.set_data(key, {"workout_id": 5})

    assert await storage.get_data(key) == {"workout_id": 5}
    assert len(json.loads(path.read_text())) == 1


# ---------- concurrent writes: the actual disk write must be serialized ----------


async def test_many_concurrent_writers_do_not_crash_or_lose_updates(tmp_path):
    """The write moved to a worker thread (asyncio.to_thread) so the event
    loop isn't blocked on file I/O for every set logged. Without a lock
    serializing the actual write, two concurrent writers race on the same
    shared ".tmp" path and whichever calls os.replace() second raises
    FileNotFoundError — and even if that didn't crash, an unordered write
    could silently drop an update.
    """
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))

    async def writer(uid: int) -> None:
        key = _key(uid)
        for i in range(20):
            await storage.set_data(key, {"n": i})

    await asyncio.gather(*(writer(uid) for uid in range(30)))

    for uid in range(30):
        assert await storage.get_data(_key(uid)) == {"n": 19}
    assert len(json.loads(path.read_text())) == 30


async def test_drop_user_forgets_state_and_data_but_spares_others(tmp_path):
    """Пара к db.wipe_user_account: после сноса аккаунта от диалога не остаётся
    ни состояния, ни черновиков — и только у снесённого."""
    path = tmp_path / "fsm.json"
    storage = JSONFileStorage(str(path))
    await storage.set_state(_key(1), _Flow.waiting)
    await storage.set_data(_key(1), {"ai_program_draft": {"name": "Масса 4× верх/низ"}})
    await storage.set_data(_key(2), {"ai_program_draft": {"name": "Чужое"}})

    dropped = await storage.drop_user(1)

    assert dropped == 1
    assert await storage.get_state(_key(1)) is None
    assert await storage.get_data(_key(1)) == {}
    assert await storage.get_data(_key(2)) == {"ai_program_draft": {"name": "Чужое"}}
    # И на диске тоже: файл переживает перезапуск, значит и снос должен.
    assert json.loads(path.read_text()) == {
        json.dumps({"bot_id": 1, "business_connection_id": None, "chat_id": 2,
                    "destiny": "default", "thread_id": None, "user_id": 2}, sort_keys=True):
        {"data": {"ai_program_draft": {"name": "Чужое"}}},
    }
