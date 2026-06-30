from aiogram.fsm.storage.base import StorageKey

from fsm_storage import JSONFileStorage


def _key():
    return StorageKey(bot_id=1, chat_id=2, user_id=3)


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
