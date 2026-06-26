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

from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.fsm.state import State


def _key_to_str(key: StorageKey) -> str:
    return json.dumps(asdict(key), sort_keys=True)


class JSONFileStorage(BaseStorage):
    def __init__(self, path: str):
        self._path = path
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if os.path.exists(self._path):
            with open(self._path, "r") as f:
                return json.load(f)
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
