"""FSM storage backed by a JSON file on the persistent volume.

MemoryStorage loses all in-flight conversation state on every restart, which
on a redeploy leaves users with stale inline keyboards whose callbacks no
longer match any StateFilter. This storage persists state/data to disk so
restarts don't silently break buttons mid-flow.
"""
import json
import os
from dataclasses import asdict
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


def _key_to_str(key: StorageKey) -> str:
    return json.dumps(asdict(key), sort_keys=True)


def _restore_int_keys(obj: Any) -> Any:
    """Undo JSON's stringification of dict keys for FSM data like ``{exercise_id: block_id}``.

    Handlers build these dicts with int keys (exercise/block ids) and look them up
    the same way, but ``json.dump`` silently turns those keys into strings. Without
    this, every dict survives a save/load round-trip (e.g. a bot restart) with keys
    that no longer match an int lookup, so set logging breaks for any in-progress
    workout.
    """
    if isinstance(obj, dict):
        return {
            (int(k) if k.lstrip("-").isdigit() else k): _restore_int_keys(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_restore_int_keys(v) for v in obj]
    return obj


class JSONFileStorage(BaseStorage):
    def __init__(self, path: str):
        self._path = path
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                return _restore_int_keys(json.load(f))
        return {}

    def _save(self) -> None:
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._data, f)
        os.replace(tmp_path, self._path)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        entry = self._data.setdefault(_key_to_str(key), {})
        if state is None:
            entry["state"] = None
        elif isinstance(state, State):
            entry["state"] = state.state
        else:
            entry["state"] = str(state)
        self._save()

    async def get_state(self, key: StorageKey) -> str | None:
        return self._data.get(_key_to_str(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: dict) -> None:
        entry = self._data.setdefault(_key_to_str(key), {})
        entry["data"] = dict(data)
        self._save()

    async def get_data(self, key: StorageKey) -> dict:
        return dict(self._data.get(_key_to_str(key), {}).get("data", {}))

    async def close(self) -> None:
        self._save()
