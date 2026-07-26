"""SQLite-backed FSM storage.

aiogram's default ``MemoryStorage`` is wrong for this deployment: the Fly
machine is stopped whenever it is idle, so a wizard started before the machine
slept would come back with its state erased. Persisting to the same SQLite file
as everything else keeps multi-step dialogs intact across sleep/wake.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from . import db


def _key(key: StorageKey) -> str:
    return f"{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{key.destiny}"


class SqliteStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        db.db().execute(
            "INSERT INTO fsm(key, state, ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state, ts=excluded.ts",
            (_key(key), value, int(time.time())),
        )

    async def get_state(self, key: StorageKey) -> str | None:
        row = db.db().execute("SELECT state FROM fsm WHERE key=?", (_key(key),)).fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        db.db().execute(
            "INSERT INTO fsm(key, data, ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, ts=excluded.ts",
            (_key(key), json.dumps(dict(data), ensure_ascii=False), int(time.time())),
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = db.db().execute("SELECT data FROM fsm WHERE key=?", (_key(key),)).fetchone()
        if not row or not row["data"]:
            return {}
        try:
            value = json.loads(row["data"])
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    async def close(self) -> None:
        return None
