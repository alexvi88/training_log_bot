"""FSM storage backed by a JSON file on the persistent volume.

MemoryStorage loses all in-flight conversation state on every restart, which
on a redeploy leaves users with stale inline keyboards whose callbacks no
longer match any StateFilter. This storage persists state/data to disk so
restarts don't silently break buttons mid-flow.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

logger = logging.getLogger(__name__)


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
        # Guards the actual disk write. The original code ran open()/json.dump()/
        # os.replace() synchronously with no `await` inside it, so — cooperative
        # scheduling being what it is — two calls could never interleave. Moving
        # the write to a worker thread (see _save) drops that guarantee for free:
        # without this lock, two concurrent writers race on the same *shared*
        # ".tmp" path, and whichever calls os.replace() second raises
        # FileNotFoundError because the first already moved it away.
        self._save_lock = asyncio.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r") as f:
                return _restore_int_keys(json.load(f))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # A truncated write (kill mid-save, full disk, a bad restore) leaves
            # a file that will never parse — and this runs from __init__, called
            # from Dispatcher(storage=...) in main(), so an unguarded raise here
            # takes the whole bot down before it can serve a single update.
            # Losing in-flight FSM state is recoverable (workout.py rebuilds an
            # active workout's open exercises/blocks straight from the DB, see
            # _reopen_exercises); refusing to start over it is not.
            logger.exception(
                "FSM state file %s is corrupt — starting with empty state. "
                "Renaming it aside so this doesn't repeat silently.",
                self._path,
            )
            self._quarantine_corrupt_file()
            return {}

    def _quarantine_corrupt_file(self) -> None:
        # Timestamped rather than a fixed ".corrupt" suffix so a second failure
        # (e.g. corruption recurring after a restart) doesn't silently clobber
        # the evidence of the first one.
        quarantine_path = f"{self._path}.corrupt.{int(time.time())}"
        try:
            os.replace(self._path, quarantine_path)
        except OSError:
            logger.exception("Could not move aside corrupt FSM file %s", self._path)

    def _prune_if_empty(self, key_str: str) -> None:
        """Drop an entry once it carries neither a state nor any data.

        state.clear() at the end of every flow (finishing a workout, backing
        out to the menu, ...) leaves exactly this behind — a key with
        state=None, data={}. Without pruning, every user who has ever
        interacted with the bot even once stays in the file forever: it only
        ever grows, and every single write (set_state/set_data — one per
        logged set) rewrites the whole thing.
        """
        entry = self._data.get(key_str)
        if entry is not None and not entry.get("state") and not entry.get("data"):
            del self._data[key_str]

    def _write_to_disk(self, snapshot: dict[str, dict[str, Any]]) -> None:
        tmp_path = self._path + ".tmp"
        # The volume backing self._path is a network mount, which occasionally
        # surfaces a stale file handle (errno 116) on an open()/replace() pair
        # after the server-side mount is remounted underneath us. The stale
        # handle is tied to the old lookup, not to the path itself, so a fresh
        # open() a moment later resolves cleanly — retry a couple of times
        # before giving up.
        last_exc: OSError | None = None
        for attempt in range(3):
            try:
                with open(tmp_path, "w") as f:
                    json.dump(snapshot, f)
                os.replace(tmp_path, self._path)
                return
            except OSError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
        raise last_exc

    async def _save(self) -> None:
        # The snapshot is taken outside the lock (mutations to self._data are
        # synchronous with no `await`, so it's always current) but the actual
        # write is serialized by it — see _save_lock's docstring for why that
        # matters once the write moved to a worker thread.
        snapshot = {k: dict(v) for k, v in self._data.items()}
        async with self._save_lock:
            try:
                await asyncio.to_thread(self._write_to_disk, snapshot)
            except OSError:
                # self._data (the in-memory source of truth) is already up to
                # date regardless of whether the write succeeded, so a
                # persistent disk failure here shouldn't take down the handler
                # that's mid-flow (e.g. state.set_state from a callback) —
                # that would leave the user's button press unanswered on every
                # single update until the mount recovers. Losing this one
                # snapshot just means a restart during the outage would replay
                # slightly stale state, which is the same trade-off _load
                # already makes for a corrupt file.
                logger.exception(
                    "Failed to persist FSM state to %s; continuing with "
                    "in-memory state only.",
                    self._path,
                )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        key_str = _key_to_str(key)
        entry = self._data.setdefault(key_str, {})
        if state is None:
            entry["state"] = None
        elif isinstance(state, State):
            entry["state"] = state.state
        else:
            entry["state"] = str(state)
        self._prune_if_empty(key_str)
        await self._save()

    async def get_state(self, key: StorageKey) -> str | None:
        return self._data.get(_key_to_str(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: dict) -> None:
        key_str = _key_to_str(key)
        entry = self._data.setdefault(key_str, {})
        entry["data"] = dict(data)
        self._prune_if_empty(key_str)
        await self._save()

    async def get_data(self, key: StorageKey) -> dict:
        return dict(self._data.get(_key_to_str(key), {}).get("data", {}))

    async def close(self) -> None:
        await self._save()
